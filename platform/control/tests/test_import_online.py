"""Leaving Vercel: `python -m app.import_online` brings the online workspace's users,
GitHub App, connections and planned projects onto the box, and cx-control's next start
deploys them. Turso is faked at its one HTTP call by a real SQLite database holding the
online tables, answering in Turso's own wire format."""
import asyncio
import base64
import hashlib
import os
import sqlite3
import types

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient

from app import auth, db, engine, import_online, main, mailer, metrics

# A real Better Auth 1.7.7 hash, made by the repo's own dependency:
#   import { hashPassword } from "better-auth/dist/crypto/index.mjs"
#   await hashPassword("Grüße, ﬁsh 42!")      // ﬁ is the U+FB01 ligature
BA_PASSWORD = "Grüße, ﬁsh 42!"
BA_HASH = ("30c2b7642d1b04303b1ccc77513dae72:52c1aaf3d3967a4d6565187b4519186f5811c85b5389"
           "1e4e54fbfc6b3b74cb305b682f43a05dee3e0dced7b503334ab584c6ea02b587adabd1283986fdea"
           "577b")

# A setting encrypted by Node with workspace/secrets.js's rules (node:crypto,
# aes-256-gcm, key sha256("cicatrixa-settings\0" + secret), "enc1:" + base64url).
SECRET = "test-better-auth-secret-0123456789abcdef"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIfakeé\n-----END RSA PRIVATE KEY-----\n"
PEM_STORED = ("enc1:Ezc6P8jkFSsX_TG6TiknJkPhDjKqNhGX2r1WmaT14Ax39LQgZ27mvRWOvXR_pnHZWBART-ODgb"
              "EqFpfHhA4v_NGzuVLCtY_HK4iQ3H5HIQzNU18swZ1_ow-Af9Rs1ps0-L-pXg")

ENV = {"TURSO_DATABASE_URL": "libsql://cicatrixa-prod-test.aws-us-east-1.turso.io",
       "TURSO_AUTH_TOKEN": "turso-token", "BETTER_AUTH_SECRET": SECRET,
       "ADMIN_EMAILS": "owner@example.com"}

ONLINE_SCHEMA = [
    # accounts/schema.js
    'create table "user" ("id" text not null primary key, "name" text not null, "email" text '
    'not null unique, "emailVerified" integer not null, "image" text, "createdAt" date not '
    'null, "updatedAt" date not null)',
    'create table "account" ("id" text not null primary key, "accountId" text not null, '
    '"providerId" text not null, "userId" text not null references "user" ("id") on delete '
    'cascade, "accessToken" text, "refreshToken" text, "idToken" text, "accessTokenExpiresAt" '
    'date, "refreshTokenExpiresAt" date, "scope" text, "password" text, "createdAt" date not '
    'null, "updatedAt" date not null)',
]
WORKSPACE_SCHEMA = [
    # workspace/store.js
    "create table cx_setting (key text primary key, value text not null, updatedAt text not null)",
    'create table cx_github (userId text not null primary key references "user"("id") on '
    "delete cascade, installationId integer not null unique, accountLogin text not null, "
    "accountType text not null, provedBy text not null, createdAt text not null)",
    'create table cx_project (id text primary key, userId text not null references "user"("id")'
    " on delete cascade, name text not null, slug text not null unique, createdAt text not null)",
    "create table cx_service (id text primary key, projectId text not null references "
    "cx_project(id) on delete cascade, name text not null, slug text not null unique, repoFull "
    "text not null, branch text not null, isPrimary integer not null default 0, status text not "
    "null default 'queued', sha text, plan text, log text, error text, updatedAt text not null, "
    "createdAt text not null)",
]
WHEN = "2026-10-19T10:00:00.000Z"


def encrypt(plaintext: str) -> str:
    key = hashlib.sha256(f"cicatrixa-settings\0{SECRET}".encode()).digest()
    iv = os.urandom(12)
    blob = iv + AESGCM(key).encrypt(iv, plaintext.encode(), None)
    return "enc1:" + base64.urlsafe_b64encode(blob).decode().rstrip("=")


def wire(v):
    """A value as Turso's HTTP API sends it."""
    if v is None:
        return {"type": "null"}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, bytes):
        return {"type": "blob", "base64": base64.b64encode(v).decode()}
    return {"type": "text", "value": v}


class FakeTurso:
    def __init__(self, workspace=True):
        self.db = sqlite3.connect(":memory:")
        for stmt in ONLINE_SCHEMA + (WORKSPACE_SCHEMA if workspace else []):
            self.db.execute(stmt)
        self.calls = []

    def __call__(self, base, token, body):
        self.calls.append((base, token, body))
        results = []
        for req in body["requests"]:
            if req["type"] == "close":
                results.append({"type": "ok", "response": {"type": "close"}})
                continue
            try:
                cur = self.db.execute(req["stmt"]["sql"])
            except sqlite3.Error as exc:
                results.append({"type": "error", "error": {"message": str(exc)}})
                continue
            results.append({"type": "ok", "response": {"type": "execute", "result": {
                "cols": [{"name": d[0], "decltype": None} for d in cur.description or []],
                "rows": [[wire(v) for v in row] for row in cur.fetchall()],
                "affected_row_count": 0, "last_insert_rowid": None}}})
        return {"baton": None, "base_url": None, "results": results}

    # ---- the online rows, shaped as accounts/ and workspace/ write them ----

    def user(self, uid, email, verified=1, password=BA_HASH):
        self.db.execute('insert into "user" values (?,?,?,?,NULL,?,?)',
                        (uid, email.split("@")[0], email, verified, WHEN, WHEN))
        if password is not None:
            self.db.execute("insert into account (id, accountId, providerId, userId, password, "
                            "createdAt, updatedAt) values (?,?,'credential',?,?,?,?)",
                            (f"acc-{uid}", uid, uid, password, WHEN, WHEN))

    def github(self, uid, installation_id, login):
        self.db.execute("insert into cx_github values (?,?,?,'User',?,?)",
                        (uid, installation_id, login, login, WHEN))

    def project(self, pid, uid, name, slug, services, when=WHEN):
        """services: [(name, slug, repo, branch)], the first one primary."""
        self.db.execute("insert into cx_project values (?,?,?,?,?)", (pid, uid, name, slug, when))
        for i, (sname, sslug, repo, branch) in enumerate(services):
            self.db.execute("insert into cx_service (id, projectId, name, slug, repoFull, branch, "
                            "isPrimary, status, plan, updatedAt, createdAt) "
                            "values (?,?,?,?,?,?,?,'planned','{}',?,?)",
                            (f"{pid}-s{i}", pid, sname, sslug, repo, branch,
                             1 if i == 0 else 0, when, when))

    def app(self, app_id="424242"):
        for key, value in {"gh_app_id": app_id, "gh_app_slug": "cicatrixa",
                           "gh_app_name": "Cicatrixa", "gh_app_client_id": "Iv1.abc",
                           "gh_app_pem": PEM_STORED,
                           "gh_app_webhook_secret": encrypt("hook-secret"),
                           "gh_app_client_secret": encrypt("client-secret")}.items():
            self.db.execute("insert into cx_setting values (?,?,?)", (key, value, WHEN))


@pytest.fixture
def turso(fresh_db, monkeypatch):
    fake = FakeTurso()
    monkeypatch.setattr(import_online, "_post_pipeline", fake)
    return fake


def run(**kw):
    return import_online.run(env=ENV, **kw)


def online_world(t: FakeTurso):
    """The owner (admin, an org App installation, a two-service project) and Ada."""
    t.app()
    t.user("u-owner", "Owner@Example.com")
    t.user("u-ada", "ada@example.com")
    t.github("u-owner", 1001, "acme")
    t.github("u-ada", 1002, "ada")
    t.project("p-shop", "u-owner", "shop", "shop",
              [("shop", "shop", "acme/shop", "main"), ("api", "shop-api", "acme/api", "dev")])
    t.project("p-blog", "u-ada", "blog", "blog", [("blog", "blog", "ada/blog", "main")])


def count(table):
    return db.one(f"SELECT COUNT(*) c FROM {table}")["c"]


def box_user(email):
    return db.one("SELECT * FROM users WHERE email=?", (email,))


def log_in(client, email, password):
    return client.post("/login", data={"email": email, "password": password},
                       follow_redirects=False)


@pytest.fixture
def client(fresh_db, monkeypatch):
    monkeypatch.setattr(metrics, "ensure_fresh", lambda: None)
    return TestClient(main.app)          # no `with`: startup talks to Docker


# ---------- Better Auth's password hashes ----------

def test_a_better_auth_hash_accepts_its_password():
    assert auth.verify_password(BA_PASSWORD, BA_HASH)


def test_a_better_auth_hash_refuses_any_other():
    assert not auth.verify_password("Grüße, fish 43!", BA_HASH)
    assert not auth.verify_password("", BA_HASH)


def test_a_better_auth_hash_compares_passwords_in_nfkc_form_as_better_auth_does():
    assert auth.verify_password("Grüße, fish 42!", BA_HASH)          # "fi", no ligature
    assert auth.verify_password("Grüße, ﬁsh 42!", BA_HASH)     # u + combining ¨


def test_the_platforms_own_hashes_still_check_and_nonsense_never_does():
    assert auth.verify_password("correct horse", auth.hash_password("correct horse"))
    assert not auth.verify_password("x", "abc:zz")
    assert not auth.verify_password("x", "")


# ---------- reading Turso ----------

def test_a_setting_node_encrypted_decrypts_here():
    assert import_online.decrypt_setting(SECRET, PEM_STORED) == PEM
    assert import_online.decrypt_setting(SECRET, "plain value") == "plain value"


def test_a_setting_under_another_secret_or_tampered_with_does_not_decrypt():
    with pytest.raises(ValueError, match="BETTER_AUTH_SECRET"):
        import_online.decrypt_setting("another secret", PEM_STORED)
    flipped = PEM_STORED[:-3] + ("A" if PEM_STORED[-3] != "A" else "B") + PEM_STORED[-2:]
    with pytest.raises(ValueError):
        import_online.decrypt_setting(SECRET, flipped)


def test_turso_urls_become_its_http_api():
    assert (import_online.turso_http_url("libsql://db-org.aws-us-east-1.turso.io")
            == "https://db-org.aws-us-east-1.turso.io")
    assert import_online.turso_http_url("https://db-org.turso.io/") == "https://db-org.turso.io"
    assert import_online.turso_http_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
    with pytest.raises(import_online.Refused):
        import_online.turso_http_url("not a url")


def test_the_http_call_is_tursos_pipeline_with_the_token(monkeypatch):
    seen = {}

    def fake_post(url, json, headers, timeout):
        seen.update(url=url, json=json, headers=headers)
        return httpx.Response(200, json={"results": [
            {"type": "ok", "response": {"type": "execute", "result": {
                "cols": [{"name": "i"}, {"name": "f"}, {"name": "t"}, {"name": "n"},
                         {"name": "b"}],
                "rows": [[{"type": "integer", "value": "9007199254740993"},
                          {"type": "float", "value": 1.5}, {"type": "text", "value": "x"},
                          {"type": "null"}, {"type": "blob", "base64": "aGk="}]]}}},
            {"type": "ok", "response": {"type": "close"}}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    [rows] = import_online.query("https://db.turso.io", "tok", ["select 1"])
    assert seen["url"] == "https://db.turso.io/v2/pipeline"
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert seen["json"] == {"requests": [{"type": "execute", "stmt": {"sql": "select 1"}},
                                         {"type": "close"}]}
    assert rows == [{"i": 9007199254740993, "f": 1.5, "t": "x", "n": None, "b": b"hi"}]


def test_a_refused_token_or_a_failed_statement_stops_the_import(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(401, text="no"))
    with pytest.raises(import_online.Refused, match="TURSO_AUTH_TOKEN"):
        import_online.query("https://db.turso.io", "bad", ["select 1"])
    fake = FakeTurso()
    monkeypatch.setattr(import_online, "_post_pipeline", fake)
    with pytest.raises(import_online.Refused, match="no such table"):
        import_online.query("https://db.turso.io", "tok", ["select * from nowhere"])


# ---------- the import ----------

def test_a_fresh_import_brings_everyone_over_with_working_passwords(turso, client):
    online_world(turso)
    report = run()
    assert (report.users_new, report.connections_new) == (2, 2)
    assert (report.projects_new, report.services_new) == (2, 3)
    owner = box_user("owner@example.com")                   # lowercased, as login looks it up
    assert owner["external_id"] == "u-owner" and owner["email_verified"] == 1
    assert owner["pw_hash"] == BA_HASH                       # unchanged
    assert abs(owner["created_at"] - db.now()) < 60          # the trial starts today
    r = log_in(client, "ada@example.com", BA_PASSWORD)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard"
    assert auth.session_user_id(r.cookies.get(auth.COOKIE_NAME)) == box_user("ada@example.com")["id"]
    assert "Wrong email or password" in log_in(client, "ada@example.com", "guess").text


def test_projects_keep_their_addresses_repos_and_branches_and_wait_as_new(turso):
    online_world(turso)
    run()
    shop = db.one("SELECT * FROM projects WHERE external_id='p-shop'")
    assert shop["slug"] == "shop" and shop["status"] == "new"
    assert shop["user_id"] == box_user("owner@example.com")["id"]
    services = [dict(s) for s in db.all_(
        "SELECT name, slug, repo_full, branch, status FROM services WHERE project_id=? "
        "ORDER BY id", (shop["id"],))]
    assert services == [
        {"name": "shop", "slug": "shop", "repo_full": "acme/shop", "branch": "main", "status": "new"},
        {"name": "api", "slug": "shop-api", "repo_full": "acme/api", "branch": "dev", "status": "new"}]
    assert count("deployments") == 0
    conn = db.one("SELECT * FROM github_connections WHERE user_id=?", (shop["user_id"],))
    assert (conn["kind"], conn["installation_id"], conn["gh_login"]) == ("app", 1001, "acme")


def test_the_github_app_comes_over_decrypted_and_nothing_else_does(turso):
    online_world(turso)
    turso.db.execute("insert into cx_setting values ('secret_key', 'not-yours', ?)", (WHEN,))
    report = run()
    assert db.setting("gh_app_id") == "424242" and db.setting("gh_app_slug") == "cicatrixa"
    assert db.setting("gh_app_pem") == PEM
    assert db.setting("gh_app_webhook_secret") == "hook-secret"
    assert db.setting("gh_app_client_secret") == "client-secret"
    assert db.setting("secret_key") != "not-yours"
    assert "copied" in report.app


def test_admin_needs_admin_emails_and_an_email_confirmed_online(turso):
    turso.user("u-owner", "owner@example.com", verified=1)
    turso.user("u-squat", "squat@example.com", verified=0)
    turso.user("u-ada", "ada@example.com", verified=1)
    report = import_online.run(env={**ENV, "ADMIN_EMAILS": "owner@example.com,squat@example.com"})
    assert box_user("owner@example.com")["is_admin"] == 1
    assert box_user("squat@example.com")["is_admin"] == 0      # typed, never confirmed
    assert box_user("squat@example.com")["email_verified"] == 0
    assert box_user("ada@example.com")["is_admin"] == 0
    assert report.admins == ["owner@example.com"]


def test_without_admin_emails_nobody_is_made_admin_and_the_summary_says_how(turso):
    turso.user("u-owner", "owner@example.com")
    report = import_online.run(env={**ENV, "ADMIN_EMAILS": ""})
    assert box_user("owner@example.com")["is_admin"] == 0
    assert "app.promote" in report.summary()


def test_an_account_already_on_the_box_keeps_its_password(turso, client):
    uid = db.q("INSERT INTO users(email,pw_hash,email_verified,created_at) VALUES(?,?,1,?)",
               ("ada@example.com", auth.hash_password("box password"), db.now())).lastrowid
    before = box_user("ada@example.com")["pw_hash"]
    turso.user("u-ada", "Ada@Example.com")
    turso.project("p-blog", "u-ada", "blog", "blog", [("blog", "blog", "ada/blog", "main")])
    report = run()
    assert report.users_linked == ["ada@example.com"] and report.users_new == 0
    row = box_user("ada@example.com")
    assert row["id"] == uid and row["pw_hash"] == before and row["external_id"] == "u-ada"
    assert db.one("SELECT user_id FROM projects WHERE external_id='p-blog'")["user_id"] == uid
    assert log_in(client, "ada@example.com", "box password").status_code == 303


def test_running_it_again_adds_nothing_twice(turso):
    online_world(turso)
    run()
    snapshot = {t: count(t) for t in ("users", "github_connections", "projects", "services")}
    again = run()
    assert {t: count(t) for t in snapshot} == snapshot
    assert (again.users_new, again.connections_new, again.projects_new) == (0, 0, 0)
    assert (again.users_before, again.connections_before, again.projects_before) == (2, 2, 2)
    assert "already here" in again.app


def test_a_second_run_brings_only_what_is_new(turso):
    online_world(turso)
    run()
    turso.user("u-late", "late@example.com")
    turso.project("p-late", "u-late", "late", "late", [("late", "late", "late/app", "main")])
    again = run()
    assert (again.users_new, again.projects_new, again.services_new) == (1, 1, 1)
    assert count("projects") == 3


def test_an_address_taken_on_the_box_skips_that_project_and_says_so(turso):
    online_world(turso)
    other = db.q("INSERT INTO users(email,pw_hash,created_at) VALUES('x@example.com','h',?)",
                 (db.now(),)).lastrowid
    db.q("INSERT INTO projects(user_id,name,slug,created_at) VALUES(?,?,?,?)",
         (other, "mine", "shop-api", db.now()))
    report = run()
    assert db.one("SELECT 1 FROM projects WHERE external_id='p-shop'") is None
    assert db.one("SELECT 1 FROM projects WHERE external_id='p-blog'") is not None
    assert report.projects_new == 1
    [why] = report.projects_skipped
    assert why.startswith("shop (shop):") and "shop-api." in why and "taken" in why
    assert "shop-api" in report.summary()


def test_an_installation_connected_to_another_account_here_is_not_imported(turso):
    online_world(turso)
    other = db.q("INSERT INTO users(email,pw_hash,created_at) VALUES('x@example.com','h',?)",
                 (db.now(),)).lastrowid
    db.q("INSERT INTO github_connections(user_id,kind,installation_id,gh_login,created_at) "
         "VALUES(?,?,?,?,?)", (other, "app", 1001, "acme", db.now()))
    report = run()
    owner = box_user("owner@example.com")
    assert owner is not None                                  # the person still comes over
    assert db.one("SELECT 1 FROM github_connections WHERE user_id=?", (owner["id"],)) is None
    assert db.one("SELECT user_id FROM github_connections WHERE installation_id=1001")[
        "user_id"] == other
    assert report.connections_new == 1
    assert any("installation 1001" in s for s in report.connections_skipped)


def test_a_box_with_a_different_github_app_refuses_and_writes_nothing(turso):
    online_world(turso)
    db.set_setting("gh_app_id", "999")
    with pytest.raises(import_online.Refused, match="different GitHub App"):
        run()
    assert count("users") == 0 and count("projects") == 0
    assert db.setting("gh_app_id") == "999" and db.setting("gh_app_pem") is None


def test_a_wrong_better_auth_secret_refuses_and_writes_nothing(turso):
    online_world(turso)
    with pytest.raises(import_online.Refused, match="BETTER_AUTH_SECRET"):
        import_online.run(env={**ENV, "BETTER_AUTH_SECRET": "not the one Vercel has"})
    assert count("users") == 0 and db.setting("gh_app_id") is None


def test_a_dry_run_reports_everything_and_writes_nothing(turso):
    online_world(turso)
    report = run(dry_run=True)
    assert (report.users_new, report.projects_new, report.services_new) == (2, 2, 3)
    assert "DRY RUN" in report.summary()
    for table in ("users", "github_connections", "projects", "services"):
        assert count(table) == 0
    assert db.setting("gh_app_id") is None
    assert run().users_new == 2                                # and the real run still can


def test_a_database_before_migration_3_is_refused(turso):
    online_world(turso)
    db.migrate_down(2)
    with pytest.raises(import_online.Refused, match="schema version 2"):
        run()
    assert turso.calls == []                                   # not even read


def test_missing_settings_are_named(fresh_db):
    with pytest.raises(import_online.Refused, match="BETTER_AUTH_SECRET"):
        import_online.run(env={**ENV, "BETTER_AUTH_SECRET": ""})


def test_a_user_without_a_password_online_gets_one_nobody_knows(turso, client):
    turso.user("u-nopw", "nopw@example.com", password=None)
    report = run()
    row = box_user("nopw@example.com")
    assert report.no_password == ["nopw@example.com"] and row["pw_hash"]
    assert "Wrong email or password" in log_in(client, "nopw@example.com", "").text


def test_a_database_the_workspace_never_touched_still_brings_its_users(fresh_db, monkeypatch):
    fake = FakeTurso(workspace=False)
    fake.user("u-ada", "ada@example.com")
    monkeypatch.setattr(import_online, "_post_pipeline", fake)
    report = run()
    assert report.users_new == 1 and report.projects_new == 0 and count("projects") == 0


def test_a_database_without_better_auth_is_the_wrong_one(fresh_db, monkeypatch):
    fake = FakeTurso()
    fake.db.execute('drop table "account"')
    monkeypatch.setattr(import_online, "_post_pipeline", fake)
    with pytest.raises(import_online.Refused, match="Better Auth"):
        run()


def test_a_malformed_online_row_is_skipped_not_trusted(turso):
    turso.user("u-ada", "ada@example.com")
    turso.project("p-bad", "u-ada", "bad", "bad`)||Host(`x", [("bad", "bad", "ada/bad", "main")])
    turso.project("p-ok", "u-ada", "ok", "ok", [("ok", "ok", "ada/ok", "--upload-pack=x")])
    report = run()
    assert count("projects") == 0 and len(report.projects_skipped) == 2


def test_reserved_slugs_match_the_web_apps():
    assert import_online.RESERVED_SLUGS == main.RESERVED_SLUGS


# ---------- telling people ----------

def test_notify_emails_each_owner_of_newly_imported_projects_once(turso, monkeypatch):
    online_world(turso)
    sent = []
    monkeypatch.setattr(mailer, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(mailer, "send", lambda to, subject, text, html=None:
                        sent.append((to, text)) or True)
    run(dry_run=True, notify=True)
    assert sent == []                                          # never from a dry run
    report = run(notify=True)
    assert sorted(to for to, _ in sent) == ["ada@example.com", "owner@example.com"]
    owner_text = dict(sent)["owner@example.com"]
    assert engine.service_url("shop") in owner_text and "blog" not in owner_text
    assert "emailed 2 of 2" in report.mail
    sent.clear()
    run(notify=True)
    assert sent == []                                          # nothing new, nobody twice


def test_notify_with_mail_off_says_nobody_was_emailed(turso, monkeypatch):
    online_world(turso)
    monkeypatch.setattr(mailer, "RESEND_API_KEY", "")
    assert "nobody was emailed" in run(notify=True).mail


# ---------- the command ----------

def test_the_command_prints_a_summary_or_refuses_with_exit_1(turso, monkeypatch, capsys):
    online_world(turso)
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    assert import_online.main(["--dry-run"]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    monkeypatch.setenv("BETTER_AUTH_SECRET", "")
    assert import_online.main([]) == 1
    assert "import refused, nothing was written" in capsys.readouterr().err


def test_the_command_will_not_conjure_a_database(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "nowhere.db"))
    assert import_online.main([]) == 1
    assert not (tmp_path / "nowhere.db").exists()


# ---------- the first start after the import ----------

def test_startup_deploys_only_imported_services_never_deployed(turso, monkeypatch):
    online_world(turso)
    turso.user("u-late", "late@example.com")
    turso.project("p-late", "u-late", "late", "late", [("late", "late", "late/app", "main")])
    run()
    # Late's trial ran out (and they never paid): their project waits for them.
    db.q("UPDATE users SET created_at=? WHERE email='late@example.com'", (db.now() - 30 * 86400,))
    # The blog was already tried here once; it is its owner's to retry.
    blog = db.one("SELECT s.id FROM services s JOIN projects p ON p.id=s.project_id "
                  "WHERE p.external_id='p-blog'")["id"]
    db.q("INSERT INTO deployments(service_id,status,created_at) VALUES(?,'failed',?)",
         (blog, db.now()))
    # A project made on the box itself is create_project's business, not this one's.
    owner = box_user("owner@example.com")["id"]
    native = db.q("INSERT INTO projects(user_id,name,slug,created_at) VALUES(?,?,?,?)",
                  (owner, "native", "native", db.now())).lastrowid
    db.q("INSERT INTO services(project_id,name,slug,repo_full,created_at) VALUES(?,?,?,?,?)",
         (native, "native", "native", "acme/native", db.now()))

    started = []

    async def fake_deploy(service_id, trigger="manual"):
        started.append((service_id, trigger))

    async def idle():
        return None

    monkeypatch.setattr(main.engine, "deploy", fake_deploy)
    monkeypatch.setattr(main.engine, "dock",
                        lambda: types.SimpleNamespace(networks=types.SimpleNamespace(
                            get=lambda name: None)))
    monkeypatch.setattr(main.watchdog, "run_forever", idle)
    monkeypatch.setattr(main.watchdog, "attach_loop", lambda loop: None)
    monkeypatch.setattr(main.bus, "attach_loop", lambda loop: None)

    async def boot():
        await main.startup()
        await asyncio.sleep(0)

    asyncio.run(boot())
    shop = [s["id"] for s in db.all_(
        "SELECT s.id FROM services s JOIN projects p ON p.id=s.project_id "
        "WHERE p.external_id='p-shop' ORDER BY s.id")]
    assert started == [(sid, "import") for sid in shop]
