import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """A real, empty SQLite database with the current schema applied.

    db.conn() caches per thread, so the cached handle has to go with the path
    or every test would keep talking to the first test's file.
    """
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    if hasattr(db._local, "conn"):
        del db._local.conn
    db.init()
    yield db
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        del db._local.conn
