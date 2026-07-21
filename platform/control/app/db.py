"""SQLite storage for the Cicatrixa platform control plane."""
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


MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN referral_code TEXT",
    "ALTER TABLE users ADD COLUMN referred_by INTEGER REFERENCES users(id)",
    "ALTER TABLE users ADD COLUMN referral_converted INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN paid_until REAL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_referral_code ON users(referral_code)",
]


def init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn().executescript(SCHEMA)
    for stmt in MIGRATIONS:
        try:
            conn().execute(stmt)
            conn().commit()
        except sqlite3.OperationalError:
            pass  # column/index already exists
    # additive migrations for databases created before these columns existed
    for table, col, ctype in (("users", "quota_services", "INTEGER"),
                              ("users", "quota_ram_mb", "INTEGER"),
                              ("users", "quota_disk_mb", "INTEGER"),
                              ("users", "email_verified", "INTEGER NOT NULL DEFAULT 0"),
                              ("users", "verify_code", "TEXT"),
                              ("users", "verify_expires", "REAL"),
                              ("services", "api_prefix", "TEXT")):
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
