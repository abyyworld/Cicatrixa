"""The migration runner must be loud. The hazard it replaces was a swallow that
made a broken migration indistinguishable from an applied one."""
import pytest

from app import db


def _cols(table: str) -> set[str]:
    return {r["name"] for r in db.all_(f"PRAGMA table_info({table})")}


def _head() -> int:
    """Highest shipped migration, or the legacy baseline if none ship yet.
    Tests inject their own migrations above this so they never collide with
    real ones as the list grows."""
    return max([m.version for m in db.MIGRATIONS] + [db.LEGACY_BASELINE])


def test_a_fresh_database_lands_on_the_head_of_the_migration_list(fresh_db):
    assert db.schema_version() == _head()


def test_every_shipped_migration_declares_a_real_rollback(fresh_db):
    """A migration without a `down` cannot be rolled back, which is how a bad
    deploy becomes unrecoverable."""
    for m in db.MIGRATIONS:
        assert m.down, f"migration {m.version} ({m.name}) has no down"


def test_the_shipped_migrations_roll_all_the_way_down_and_back_up(fresh_db):
    head = _head()
    assert db.migrate_down(db.LEGACY_BASELINE) == db.LEGACY_BASELINE
    for table in ("transform", "break_observation"):
        assert db.one("SELECT name FROM sqlite_master WHERE name=?", (table,)) is None
    assert "external_id" not in _cols("users") and "external_id" not in _cols("projects")
    assert db.migrate() == head
    for table in ("transform", "break_observation"):
        assert db.one("SELECT name FROM sqlite_master WHERE name=?", (table,)) is not None
    assert "external_id" in _cols("users") and "external_id" in _cols("projects")


def test_online_import_ids_are_unique_but_optional(fresh_db):
    """Migration 3: what app.import_online brought over carries the workspace's id,
    so a second run finds it. Accounts made here have none, and many may have none."""
    for email in ("a@example.com", "b@example.com"):
        db.q("INSERT INTO users(email,pw_hash,created_at) VALUES(?,?,?)", (email, "h", db.now()))
    db.q("UPDATE users SET external_id='u-1' WHERE email='a@example.com'")
    with pytest.raises(Exception, match="UNIQUE"):
        db.q("UPDATE users SET external_id='u-1' WHERE email='b@example.com'")
    db.conn().rollback()
    uid = db.one("SELECT id FROM users LIMIT 1")["id"]
    for slug in ("one", "two"):
        db.q("INSERT INTO projects(user_id,name,slug,created_at) VALUES(?,?,?,?)",
             (uid, slug, slug, db.now()))
    db.q("UPDATE projects SET external_id='p-1' WHERE slug='one'")
    with pytest.raises(Exception, match="UNIQUE"):
        db.q("UPDATE projects SET external_id='p-1' WHERE slug='two'")


def test_migration_3_rolls_back_to_2_alone(fresh_db):
    db.q("INSERT INTO users(email,pw_hash,created_at,external_id) VALUES(?,?,?,?)",
         ("a@example.com", "h", db.now(), "u-1"))
    db.q("INSERT INTO imported_project(external_id,imported_at) VALUES('p-1',?)", (db.now(),))
    assert "notified_at" in _cols("projects")
    assert db.migrate_down(2) == 2
    assert "external_id" not in _cols("users")
    assert "notified_at" not in _cols("projects") and "external_id" not in _cols("projects")
    assert db.one("SELECT name FROM sqlite_master WHERE name='imported_project'") is None
    assert db.one("SELECT email FROM users")["email"] == "a@example.com"   # rows survive
    assert db.one("SELECT name FROM sqlite_master WHERE name='transform'") is not None
    assert db.migrate() == _head()


def test_legacy_columns_still_arrive_on_a_fresh_database(fresh_db):
    # The pre-versioning DDL has to keep running or a new install would be
    # missing columns the code reads.
    assert {"referral_code", "paid_until", "email_verified"} <= _cols("users")


def test_migration_applies_and_bumps_the_version(fresh_db):
    m = db.Migration(_head() + 1, "add_widget", ["CREATE TABLE widget (id INTEGER PRIMARY KEY)"],
                     ["DROP TABLE widget"])
    assert db.migrate(migrations=[m]) == _head() + 1
    assert db.one("SELECT name FROM sqlite_master WHERE name='widget'") is not None


def test_migration_rolls_back_cleanly(fresh_db):
    m = db.Migration(_head() + 1, "add_widget", ["CREATE TABLE widget (id INTEGER PRIMARY KEY)"],
                     ["DROP TABLE widget"])
    db.migrate(migrations=[m])
    assert db.migrate_down(_head(), migrations=[m]) == _head()
    assert db.one("SELECT name FROM sqlite_master WHERE name='widget'") is None


def test_applying_twice_is_a_no_op(fresh_db):
    m = db.Migration(_head() + 1, "add_widget", ["CREATE TABLE widget (id INTEGER PRIMARY KEY)"],
                     ["DROP TABLE widget"])
    db.migrate(migrations=[m])
    assert db.migrate(migrations=[m]) == _head() + 1  # would raise "table exists" if re-run


def test_a_broken_migration_raises_instead_of_being_swallowed(fresh_db):
    """The whole point. The old code caught OperationalError and continued."""
    broken = db.Migration(_head() + 1, "broken", ["ALTER TABLE nonexistent_table ADD COLUMN x TEXT"],
                          [])
    with pytest.raises(RuntimeError, match="failed and was rolled back"):
        db.migrate(migrations=[broken])


def test_a_broken_migration_does_not_advance_the_version(fresh_db):
    broken = db.Migration(_head() + 1, "broken", ["THIS IS NOT SQL"], [])
    with pytest.raises(RuntimeError):
        db.migrate(migrations=[broken])
    assert db.schema_version() == _head()


def test_a_partially_failing_migration_leaves_nothing_behind(fresh_db):
    """Statement 1 succeeds, statement 2 fails: the table must not survive."""
    m = db.Migration(_head() + 1, "half_broken",
                     ["CREATE TABLE half (id INTEGER PRIMARY KEY)", "THIS IS NOT SQL"],
                     ["DROP TABLE half"])
    with pytest.raises(RuntimeError):
        db.migrate(migrations=[m])
    assert db.one("SELECT name FROM sqlite_master WHERE name='half'") is None
    assert db.schema_version() == _head()


def test_migrations_apply_in_version_order(fresh_db):
    ms = [
        db.Migration(_head() + 2, "second", ["ALTER TABLE step ADD COLUMN b TEXT"], ["DROP TABLE step"]),
        db.Migration(_head() + 1, "first", ["CREATE TABLE step (a TEXT)"], ["DROP TABLE step"]),
    ]
    assert db.migrate(migrations=ms) == _head() + 2  # out of order would fail
    assert _cols("step") == {"a", "b"}


def test_a_migration_cannot_claim_a_legacy_version():
    with pytest.raises(ValueError):
        db.Migration(db.LEGACY_BASELINE, "too_low", [], [])


def test_init_is_idempotent_across_restarts(fresh_db):
    """cx-control calls db.init() on every boot."""
    before = db.schema_version()
    db.init()
    db.init()
    assert db.schema_version() == before
    assert {"referral_code", "paid_until"} <= _cols("users")


def test_init_does_not_reset_a_version_set_by_a_later_migration(fresh_db):
    m = db.Migration(_head() + 1, "add_widget", ["CREATE TABLE widget (id INTEGER PRIMARY KEY)"],
                     ["DROP TABLE widget"])
    db.migrate(migrations=[m])
    db.init()  # init must never stamp us back below where we already are
    assert db.schema_version() == _head() + 1
