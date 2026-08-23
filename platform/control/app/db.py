"""SQLite storage for the Cicatrixa platform control plane."""
import contextlib
import os
import sqlite3
import threading
import time

DB_PATH = os.environ.get("DB_PATH", "/data/cicatrixa.db")

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT UNIQUE NOT NULL,
    pw_hash         TEXT NOT NULL,
    is_admin        INTEGER NOT NULL DEFAULT 0,
    quota_services  INTEGER,          -- NULL -> platform default
    quota_ram_mb    INTEGER,
    quota_disk_mb   INTEGER,
    email_verified  INTEGER NOT NULL DEFAULT 0,
    verify_code     TEXT,
    verify_expires  REAL,
    created_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS github_connections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    kind            TEXT NOT NULL,              -- 'app' | 'pat'
    installation_id INTEGER,
    pat_token       TEXT,
    gh_login        TEXT,
    created_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    name          TEXT NOT NULL,
    slug          TEXT UNIQUE NOT NULL,
    status        TEXT NOT NULL DEFAULT 'new',  -- new|deploying|live|degraded|failed|stopped
    autodeploy    INTEGER NOT NULL DEFAULT 1,
    created_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS services (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES projects(id),
    name          TEXT NOT NULL,                -- e.g. frontend, api
    slug          TEXT UNIQUE NOT NULL,         -- subdomain: <slug>.<BASE_DOMAIN>
    repo_full     TEXT NOT NULL,                -- owner/repo
    branch        TEXT NOT NULL DEFAULT 'main',
    status        TEXT NOT NULL DEFAULT 'new',  -- new|deploying|live|failed|stopped
    container     TEXT,
    image         TEXT,
    port          INTEGER,
    health_path   TEXT DEFAULT '/',
    last_sha      TEXT,
    created_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS deployments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id  INTEGER NOT NULL REFERENCES services(id),
    sha         TEXT,
    trigger     TEXT NOT NULL DEFAULT 'manual', -- manual|webhook|poll|heal
    status      TEXT NOT NULL DEFAULT 'running',-- running|success|failed
    log         TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS databases (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES projects(id),
    name          TEXT NOT NULL,
    slug          TEXT UNIQUE NOT NULL,
    engine        TEXT NOT NULL DEFAULT 'postgres16',
    db_name       TEXT NOT NULL,
    db_user       TEXT NOT NULL,
    db_password   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'new',
    container     TEXT,
    created_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    role        TEXT NOT NULL,                 -- user|agent
    kind        TEXT NOT NULL DEFAULT 'text',  -- text|fix|status
    content     TEXT NOT NULL DEFAULT '',
    data        TEXT,                          -- JSON: patches, commit_message, service, applied
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email        TEXT NOT NULL,
    token        TEXT UNIQUE NOT NULL,
    origin       TEXT NOT NULL,               -- requested|admin_sent
    status       TEXT NOT NULL DEFAULT 'pending', -- pending|approved|used|revoked
    decided_by   INTEGER REFERENCES users(id),
    created_at   REAL NOT NULL,
    decided_at   REAL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


# ---------------------------------------------------------------------------
# Schema versioning
#
# Everything below LEGACY_BASELINE predates versioning: a flat list of DDL run
# inside `except sqlite3.OperationalError: pass`. That swallow cannot tell "this
# column already exists" from "this migration is broken", which on a
# customer-facing database is a silent corruption hazard. It is kept only to
# reach the same shape on databases that already ran it.
#
# Everything from LEGACY_BASELINE + 1 onwards goes through migrate(): ordered,
# transactional, reversible, and it RAISES. Add new schema there, never below.
# ---------------------------------------------------------------------------

LEGACY_BASELINE = 1
SCHEMA_VERSION_KEY = "schema_version"

LEGACY_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN referral_code TEXT",
    "ALTER TABLE users ADD COLUMN referred_by INTEGER REFERENCES users(id)",
    "ALTER TABLE users ADD COLUMN referral_converted INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN paid_until REAL",
    "ALTER TABLE users ADD COLUMN stripe_customer_id TEXT",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_referral_code ON users(referral_code)",
]

LEGACY_COLUMNS = [
    ("users", "quota_services", "INTEGER"),
    ("users", "quota_ram_mb", "INTEGER"),
    ("users", "quota_disk_mb", "INTEGER"),
    ("users", "email_verified", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "verify_code", "TEXT"),
    ("users", "verify_expires", "REAL"),
    ("services", "api_prefix", "TEXT"),
]


class Migration:
    """One reversible schema step. `up` and `down` are lists of SQL statements."""

    def __init__(self, version: int, name: str, up: list[str], down: list[str]):
        if version <= LEGACY_BASELINE:
            raise ValueError(f"migration version must be > {LEGACY_BASELINE}")
        self.version, self.name, self.up, self.down = version, name, up, down

    def __repr__(self):
        return f"<Migration {self.version} {self.name}>"


# Ordered, strictly applied. Append only; never edit a shipped migration.
MIGRATIONS: list[Migration] = [
    Migration(
        2, "flywheel",
        up=[
            # Promoted, reusable, tenant-agnostic. Contains no customer code —
            # see flywheel.insert_transform, which enforces that on write.
            """CREATE TABLE transform (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                vendor_package           TEXT NOT NULL,
                applies_to_version_range TEXT,
                symbol_path              TEXT NOT NULL,
                break_kind               TEXT NOT NULL,
                match_pattern            TEXT NOT NULL,   -- JSON libcst matcher spec
                rewrite_ref              TEXT NOT NULL,   -- dotted name of a codemod in transforms/
                supporting_observations  TEXT NOT NULL DEFAULT '[]',  -- JSON [{id, level}]
                confidence_tier          TEXT NOT NULL DEFAULT 'candidate',
                promoted_at              REAL,
                created_at               REAL NOT NULL
            )""",
            "CREATE INDEX idx_transform_lookup ON transform(vendor_package, symbol_path, break_kind)",
            # One per incident, tenant-scoped, may contain customer specifics.
            """CREATE TABLE break_observation (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id               INTEGER NOT NULL REFERENCES users(id),
                project_id            INTEGER REFERENCES projects(id),
                service_id            INTEGER REFERENCES services(id),
                trigger               TEXT NOT NULL DEFAULT 'crash',
                    -- crash|ci_failure|dependency_bump|drift_watch
                vendor_package        TEXT,
                vendor_version_from   TEXT,
                vendor_version_to     TEXT,
                symbol_path           TEXT,
                break_kind            TEXT NOT NULL DEFAULT 'unknown',
                    -- signature_change|symbol_removed|symbol_moved|return_shape_change
                    -- |default_changed|behaviour_change|unknown
                call_site_fingerprint TEXT,
                library_lookup_result TEXT,               -- hit|miss
                matched_transform_id  INTEGER REFERENCES transform(id),
                verification_level    TEXT NOT NULL DEFAULT 'unverified_no_coverage',
                verification_evidence TEXT,               -- JSON
                pr_number             INTEGER,
                pr_url                TEXT,
                pr_branch             TEXT,
                pr_repo_full          TEXT,
                pr_opened_at          REAL,
                pr_state              TEXT,               -- open|merged|closed
                merged_at             REAL,
                human_commits_on_pr   INTEGER NOT NULL DEFAULT 0,
                created_at            REAL NOT NULL,
                updated_at            REAL
            )""",
            "CREATE INDEX idx_obs_user ON break_observation(user_id)",
            "CREATE INDEX idx_obs_match ON break_observation(vendor_package, symbol_path, break_kind)",
            "CREATE INDEX idx_obs_pr ON break_observation(pr_repo_full, pr_number)",
            "CREATE INDEX idx_obs_opened ON break_observation(pr_opened_at)",
            # Per-service PR mode. NULL inherits the platform default, so a
            # service never silently changes behaviour when that default moves.
            "ALTER TABLE services ADD COLUMN pr_mode INTEGER",
        ],
        down=[
            "DROP INDEX IF EXISTS idx_obs_opened",
            "DROP INDEX IF EXISTS idx_obs_pr",
            "DROP INDEX IF EXISTS idx_obs_match",
            "DROP INDEX IF EXISTS idx_obs_user",
            "DROP TABLE IF EXISTS break_observation",
            "DROP INDEX IF EXISTS idx_transform_lookup",
            "DROP TABLE IF EXISTS transform",
            "ALTER TABLE services DROP COLUMN pr_mode",
        ],
    ),
]


@contextlib.contextmanager
def _transaction(c: sqlite3.Connection):
    """A transaction that really covers DDL.

    sqlite3's implicit transactions only open for DML, so a CREATE/ALTER
    autocommits and cannot be rolled back — which would leave a migration
    half-applied, the exact failure this runner exists to prevent. Dropping to
    autocommit and issuing BEGIN ourselves puts the DDL inside the transaction;
    SQLite itself is fully transactional over schema changes.
    """
    previous = c.isolation_level
    c.isolation_level = None
    try:
        c.execute("BEGIN")
        yield
    except BaseException:
        c.execute("ROLLBACK")
        raise
    else:
        c.execute("COMMIT")
    finally:
        c.isolation_level = previous


def schema_version() -> int:
    row = one("SELECT value FROM settings WHERE key=?", (SCHEMA_VERSION_KEY,))
    return int(row["value"]) if row else 0


def _set_schema_version(version: int, c: sqlite3.Connection):
    c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (SCHEMA_VERSION_KEY, str(version)))


def _pending(target: int | None, migrations: list[Migration]) -> list[Migration]:
    current = schema_version()
    ceiling = target if target is not None else max(
        [m.version for m in migrations] + [current])
    return sorted((m for m in migrations if current < m.version <= ceiling),
                  key=lambda m: m.version)


def migrate(target: int | None = None, migrations: list[Migration] | None = None) -> int:
    """Apply pending migrations in order. Each runs in its own transaction and
    raises on failure, leaving schema_version at the last step that fully
    succeeded — so a broken migration is loud and the database is not half-done."""
    migrations = MIGRATIONS if migrations is None else migrations
    for m in _pending(target, migrations):
        c = conn()
        try:
            with _transaction(c):
                for stmt in m.up:
                    c.execute(stmt)
                _set_schema_version(m.version, c)
        except Exception as exc:
            raise RuntimeError(
                f"migration {m.version} ({m.name}) failed and was rolled back: {exc}"
            ) from exc
    return schema_version()


def migrate_down(target: int, migrations: list[Migration] | None = None) -> int:
    """Roll back to `target`, newest first. Same transaction and failure rules."""
    migrations = MIGRATIONS if migrations is None else migrations
    current = schema_version()
    doomed = sorted((m for m in migrations if target < m.version <= current),
                    key=lambda m: m.version, reverse=True)
    for m in doomed:
        c = conn()
        try:
            with _transaction(c):
                for stmt in m.down:
                    c.execute(stmt)
                _set_schema_version(m.version - 1, c)
        except Exception as exc:
            raise RuntimeError(
                f"rollback of migration {m.version} ({m.name}) failed: {exc}"
            ) from exc
    return schema_version()


def init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn().executescript(SCHEMA)
    _apply_legacy()
    if schema_version() < LEGACY_BASELINE:
        # Fresh database, or one created before versioning existed: both are at
        # the legacy shape once _apply_legacy has run, so stamp them there.
        c = conn()
        with _transaction(c):
            _set_schema_version(LEGACY_BASELINE, c)
    migrate()


def _apply_legacy():
    """Pre-versioning DDL. Tolerant by necessity — it has no record of what ran."""
    for stmt in LEGACY_MIGRATIONS:
        try:
            conn().execute(stmt)
            conn().commit()
        except sqlite3.OperationalError:
            pass  # column/index already exists
    for table, col, ctype in LEGACY_COLUMNS:
        try:
            conn().execute(f"ALTER TABLE {table} ADD COLUMN {col} {ctype}")
        except sqlite3.OperationalError:
            pass  # already present
    conn().commit()


def q(sql: str, args: tuple = ()):
    cur = conn().execute(sql, args)
    conn().commit()
    return cur


def one(sql: str, args: tuple = ()):
    return conn().execute(sql, args).fetchone()


def all_(sql: str, args: tuple = ()):
    return conn().execute(sql, args).fetchall()


def setting(key: str, default=None):
    row = one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str):
    q("INSERT INTO settings(key,value) VALUES(?,?) "
      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def now() -> float:
    return time.time()
