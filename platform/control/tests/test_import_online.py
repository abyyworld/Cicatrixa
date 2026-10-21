"""Leaving Vercel: `python -m app.import_online` brings the online workspace's users,
GitHub App, connections and planned projects onto the box, and cx-control's next start
deploys them. Turso is faked at its one HTTP call by a real SQLite database holding the
online tables, answering in Turso's own wire format."""
import asyncio
import base64
import hashlib
import hmac
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

    def proof(self, uid):
        """accounts/auth.js onPasswordReset: the password was set through an emailed link."""
        self.db.execute("insert into cx_setting values (?,?,?)", (f"pw_proof:{uid}", WHEN, WHEN))

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


def ba_hash(password: str) -> str:
    """A Better Auth hash, by its own rules (understand-data.md §2): scrypt N=16384 r=16
    p=1 dklen 64 over the NFKC password, the salt being the hex TEXT's bytes."""
    import unicodedata
    salt = os.urandom(16).hex()
    key = hashlib.scrypt(unicodedata.normalize("NFKC", password).encode(), salt=salt.encode(),
                         n=16384, r=16, p=1, dklen=64, maxmem=64 * 1024 * 1024)
    return f"{salt}:{key.hex()}"


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


def test_admin_needs_admin_emails_a_confirmed_email_and_a_password_set_by_emailed_link(turso):
    turso.user("u-owner", "owner@example.com", verified=1)
    turso.proof("u-owner")
    turso.user("u-squat", "squat@example.com", verified=0)
    turso.proof("u-squat")
    turso.user("u-ada", "ada@example.com", verified=1)
    turso.proof("u-ada")
    report = import_online.run(env={**ENV, "ADMIN_EMAILS": "owner@example.com,squat@example.com"})
    assert box_user("owner@example.com")["is_admin"] == 1
    assert box_user("squat@example.com")["is_admin"] == 0      # typed, never confirmed
    assert box_user("squat@example.com")["email_verified"] == 0
    assert box_user("ada@example.com")["is_admin"] == 0        # not an admin address
    assert report.admins == ["owner@example.com"]


def test_a_confirmed_admin_address_whose_password_nobody_proved_is_not_admin(turso):
    """Someone signed up as the owner's address with their own password; the owner
    clicked the confirmation link they were sent. Confirmed — and the squatter's
    password. Only a password set through an emailed reset link proves the holder."""
    turso.user("u-owner", "owner@example.com", verified=1)
    report = run()
    assert box_user("owner@example.com")["is_admin"] == 0
    assert report.admins == [] and report.admins_unproven == ["owner@example.com"]
    assert "owner@example.com is in ADMIN_EMAILS" in report.summary()
    assert "app.promote" in report.summary()


def test_a_proof_made_online_after_the_import_makes_the_admin_on_the_next_run(turso):
    turso.user("u-owner", "owner@example.com", verified=1)
    run()
    turso.proof("u-owner")
    again = run()
    assert box_user("owner@example.com")["is_admin"] == 1 and again.admins == ["owner@example.com"]


def test_without_admin_emails_nobody_is_made_admin_and_the_summary_says_how(turso):
    turso.user("u-owner", "owner@example.com")
    report = import_online.run(env={**ENV, "ADMIN_EMAILS": ""})
    assert box_user("owner@example.com")["is_admin"] == 0
    assert "app.promote" in report.summary()


def box_account(email, verified=1, created_at=None):
    return db.q("INSERT INTO users(email,pw_hash,email_verified,created_at) VALUES(?,?,?,?)",
                (email, auth.hash_password("box password"), verified,
                 created_at or db.now())).lastrowid


def ada_online(t: FakeTurso, verified=1):
    t.app()
    t.user("u-ada", "Ada@Example.com", verified=verified)
    t.github("u-ada", 7777, "ada")
    t.project("p-blog", "u-ada", "blog", "blog", [("blog", "blog", "ada/blog", "main")])


def test_an_account_already_on_the_box_is_not_merged_by_default(turso, client):
    """Whoever signed up here first with that address (the box takes sign-ups on any
    host once `*` points at it; with email off it confirms them without proof) must
    not receive the online person's GitHub installation and projects."""
    uid = box_account("ada@example.com", created_at=1760000000)
    ada_online(turso)
    report = run(dry_run=True)
    assert report.users_linked == [] and report.users_new == 0
    [why] = report.users_existing
    assert why.startswith("ada@example.com: already has an account here")
    assert "made here 2025-10-09" in why and "email confirmed" in why
    assert "0 projects" in why and "no GitHub" in why
    assert "--link-existing" in report.summary()
    report = run()
    assert box_user("ada@example.com")["external_id"] is None
    assert db.one("SELECT 1 FROM github_connections WHERE installation_id=7777") is None
    assert db.one("SELECT 1 FROM projects WHERE external_id='p-blog'") is None
    assert any("owner was not brought over" in s for s in report.projects_skipped)
    assert box_user("ada@example.com")["id"] == uid


def test_link_existing_merges_when_both_sides_confirmed_and_the_box_password_stays(
        turso, client):
    uid = box_account("ada@example.com")
    before = box_user("ada@example.com")["pw_hash"]
    ada_online(turso)
    report = run(link_existing="Ada@Example.com")
    assert report.users_linked == ["ada@example.com"] and report.users_new == 0
    row = box_user("ada@example.com")
    assert row["id"] == uid and row["pw_hash"] == before and row["external_id"] == "u-ada"
    assert db.one("SELECT user_id FROM projects WHERE external_id='p-blog'")["user_id"] == uid
    assert db.one("SELECT user_id FROM github_connections WHERE installation_id=7777")[
        "user_id"] == uid
    assert log_in(client, "ada@example.com", "box password").status_code == 303


@pytest.mark.parametrize("box_verified,online_verified,why", [
    (0, 1, "the account here never confirmed its email"),
    (1, 0, "the online account never confirmed its email")])
def test_link_existing_never_merges_a_side_that_did_not_confirm(
        turso, box_verified, online_verified, why):
    box_account("ada@example.com", verified=box_verified)
    ada_online(turso, verified=online_verified)
    report = run(link_existing="ada@example.com")
    assert report.users_linked == [] and why in report.users_existing[0]
    assert box_user("ada@example.com")["external_id"] is None
    assert db.one("SELECT 1 FROM projects WHERE external_id='p-blog'") is None


def test_a_box_squatter_with_email_off_gets_nothing_of_the_victims(turso, client, monkeypatch):
    """The review's attack: sign up here as the victim before the import, with email
    off (confirmed on the spot), and wait. Sign-ups now wait for the import, and an
    account made before them is never merged by default."""
    monkeypatch.setattr(mailer, "RESEND_API_KEY", "")
    monkeypatch.setenv("TURSO_DATABASE_URL", ENV["TURSO_DATABASE_URL"])
    r = client.post("/signup", data={"email": "victim@example.com", "password": "attacker-pw"},
                    follow_redirects=False)
    assert r.status_code == 200 and "Sign-ups open shortly" in r.text
    assert box_user("victim@example.com") is None
    squatter = box_account("victim@example.com")          # made some other way, earlier
    turso.app()
    turso.user("u-victim", "victim@example.com")
    turso.github("u-victim", 7777, "victim")
    turso.project("p-v", "u-victim", "vproj", "vproj", [("vproj", "vproj", "victim/secret", "main")])
    run()
    assert db.one("SELECT 1 FROM github_connections WHERE user_id=?", (squatter,)) is None
    assert db.one("SELECT 1 FROM projects WHERE user_id=?", (squatter,)) is None


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


def test_a_password_reset_online_since_is_carried_over(turso, client):
    online_world(turso)
    run()
    turso.db.execute("update account set password=? where userId='u-ada'",
                     (ba_hash("brand new password 99"),))
    again = run()
    assert again.users_refreshed == ["ada@example.com"]
    assert log_in(client, "ada@example.com", "brand new password 99").status_code == 303
    assert "Wrong email or password" in log_in(client, "ada@example.com", BA_PASSWORD).text
    assert run().users_refreshed == []                          # nothing new the third time


def test_a_password_chosen_here_is_never_overwritten_by_the_online_one(turso, client):
    online_world(turso)
    run()
    db.q("UPDATE users SET pw_hash=? WHERE email='ada@example.com'",
         (auth.hash_password("chosen on the box"),))
    turso.db.execute("update account set password=? where userId='u-ada'",
                     (ba_hash("brand new password 99"),))
    assert run().users_refreshed == []
    assert log_in(client, "ada@example.com", "chosen on the box").status_code == 303


def test_an_email_confirmed_online_since_is_confirmed_here(turso):
    turso.user("u-a", "a@example.com", verified=0)
    run()
    assert box_user("a@example.com")["email_verified"] == 0
    turso.db.execute("update \"user\" set emailVerified=1 where id='u-a'")
    assert run().users_confirmed == ["a@example.com"]
    assert box_user("a@example.com")["email_verified"] == 1


def test_a_project_deleted_here_never_comes_back(turso):
    online_world(turso)
    run()
    pid = db.one("SELECT id FROM projects WHERE external_id='p-blog'")["id"]
    db.q("DELETE FROM services WHERE project_id=?", (pid,))    # as engine.delete_project
    db.q("DELETE FROM projects WHERE id=?", (pid,))
    again = run()
    assert again.projects_new == 0 and again.projects_deleted_here == ["blog (blog)"]
    assert db.one("SELECT 1 FROM projects WHERE external_id='p-blog'") is None


def test_a_project_deleted_online_goes_here_too_unless_it_has_deployed(turso):
    online_world(turso)
    run()
    shop = db.one("SELECT id FROM projects WHERE external_id='p-shop'")["id"]
    shop_api = db.one("SELECT id FROM services WHERE slug='shop-api'")["id"]
    db.q("INSERT INTO deployments(service_id,status,created_at) VALUES(?,'success',?)",
         (shop_api, db.now()))
    for pid in ("p-blog", "p-shop"):
        turso.db.execute("delete from cx_service where projectId=?", (pid,))
        turso.db.execute("delete from cx_project where id=?", (pid,))
    again = run()
    assert again.projects_removed == ["blog (blog)"]
    assert db.one("SELECT 1 FROM projects WHERE slug='blog'") is None
    assert db.one("SELECT 1 FROM services WHERE slug='blog'") is None
    assert db.one("SELECT 1 FROM projects WHERE id=?", (shop,)) is not None
    assert "shop (shop): deleted online, but it has deployed here" in again.summary()


def test_without_the_online_projects_table_nothing_here_is_removed(turso, monkeypatch):
    online_world(turso)
    run()
    bare = FakeTurso(workspace=False)
    bare.user("u-owner", "owner@example.com")
    bare.user("u-ada", "ada@example.com")
    monkeypatch.setattr(import_online, "_post_pipeline", bare)
    assert run().projects_removed == [] and count("projects") == 2


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
    # Its projects would only fail at deploy ("no GitHub connection"): held back, listed.
    assert report.held_back == ["shop (shop) of owner@example.com: no GitHub connection here"]
    assert "held back" in report.summary()


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


def test_a_database_before_migration_4_is_refused(turso):
    online_world(turso)
    db.migrate_down(3)
    with pytest.raises(import_online.Refused, match="schema version 3"):
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

@pytest.fixture
def mail(monkeypatch):
    """Email on; records what would go out. `fail` makes the next sends fail."""
    box = types.SimpleNamespace(sent=[], fail=set(), pauses=[])
    monkeypatch.setattr(mailer, "RESEND_API_KEY", "re_test")

    def send(to, subject, text, html=None):
        if to in box.fail:
            mailer.last_error = "Resend answered 429: daily quota"
            return False
        box.sent.append((to, subject, text))
        return True

    monkeypatch.setattr(mailer, "send", send)
    return box


def notify(mail, **kw):
    return import_online.notify(sleep=mail.pauses.append, **kw)


def start(service_slug, deployed=False):
    """What main.startup's admission (status 'deploying') or a deploy leaves behind."""
    sid = db.one("SELECT id FROM services WHERE slug=?", (service_slug,))["id"]
    db.q("UPDATE services SET status='deploying' WHERE id=?", (sid,))
    if deployed:
        db.q("INSERT INTO deployments(service_id,status,created_at) VALUES(?,'success',?)",
             (sid, db.now()))


def test_the_import_emails_nobody(turso, mail):
    online_world(turso)
    run()
    run()
    assert mail.sent == []


def test_notify_tells_owners_once_their_projects_have_started_and_nobody_twice(turso, mail):
    online_world(turso)
    run()
    first = notify(mail)
    assert mail.sent == [] and first.not_started == 2         # nothing has started yet
    start("shop")                                             # admitted at startup
    start("blog", deployed=True)
    report = notify(mail)
    assert sorted(to for to, _, _ in mail.sent) == ["ada@example.com", "owner@example.com"]
    owner_text = next(text for to, _, text in mail.sent if to == "owner@example.com")
    assert "have started deploying" in owner_text and "being deployed" not in owner_text
    assert engine.service_url("shop") in owner_text and "blog" not in owner_text
    assert "emailed 2 of 2" in report.summary()
    assert mail.pauses == [import_online.SEND_INTERVAL]      # paced under Resend's 2/s
    mail.sent.clear()
    assert notify(mail).owed == [] and mail.sent == []


def test_a_send_that_failed_is_owed_and_retried_next_time(turso, mail):
    online_world(turso)
    run()
    start("shop")
    start("blog")
    mail.fail.add("owner@example.com")
    report = notify(mail)
    assert [to for to, _, _ in mail.sent] == ["ada@example.com"]
    assert report.failed == ["owner@example.com"] and "429" in report.summary()
    mail.fail.clear()
    mail.sent.clear()
    notify(mail)
    assert [to for to, _, _ in mail.sent] == ["owner@example.com"]


def test_notify_stops_when_every_send_fails(turso, mail):
    for i in range(5):
        turso.user(f"u{i}", f"u{i}@example.com")
        turso.github(f"u{i}", 100 + i, f"u{i}")
        turso.project(f"p{i}", f"u{i}", f"p{i}", f"p{i}", [(f"p{i}", f"p{i}", f"u{i}/r", "main")])
    run()
    for i in range(5):
        start(f"p{i}")
    mail.fail.update(f"u{i}@example.com" for i in range(5))
    report = notify(mail)
    assert report.stopped and len(report.failed) == import_online.FAILURES_IN_A_ROW
    assert "run --notify again later" in report.summary()


def test_notify_tells_nobody_who_cannot_deploy(turso, mail):
    online_world(turso)
    run()
    start("blog", deployed=True)
    db.q("UPDATE users SET created_at=? WHERE email='ada@example.com'", (db.now() - 40 * 86400,))
    assert notify(mail).owed == [] and mail.sent == []


def test_notify_with_mail_off_refuses_and_says_what_to_set(turso, monkeypatch):
    online_world(turso)
    run()
    monkeypatch.setattr(mailer, "RESEND_API_KEY", "")
    with pytest.raises(import_online.Refused, match="RESEND_API_KEY"):
        import_online.notify()
    start("blog")
    assert import_online.notify(dry_run=True).owed == ["ada@example.com: blog"]


def test_the_import_warns_loudly_when_mail_is_off(turso, monkeypatch):
    online_world(turso)
    monkeypatch.setattr(mailer, "RESEND_API_KEY", "")
    assert "⚠ RESEND_API_KEY is empty here" in run(dry_run=True).summary()
    monkeypatch.setattr(mailer, "RESEND_API_KEY", "re_test")
    assert "RESEND_API_KEY is empty" not in run(dry_run=True).summary()


# ---------- this box's limits ----------

def test_the_import_prints_what_waits_against_the_limits_here(turso):
    turso.user("u-a", "a@example.com")
    turso.github("u-a", 1, "a")
    turso.project("p", "u-a", "shop", "shop", [("shop", "shop", "a/shop", "main"),
                                               ("api", "shop-api", "a/api", "main"),
                                               ("worker", "shop-worker", "a/worker", "main")])
    report = run(dry_run=True)
    text = report.summary()
    assert "capacity: 3 imported services wait to deploy" in text
    assert "allows 40 services" in text and "8 more can run at 768MB each" in text
    assert "a@example.com: 3 to deploy, their RAM quota (2048MB) runs 2: the rest wait" in text


def test_the_import_says_when_the_instance_service_limit_is_passed(turso):
    metrics.set_global_limits(2, 10, 6144)
    online_world(turso)
    text = run(dry_run=True).summary()
    assert "over the instance's service limit" in text


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


def test_the_command_notifies_only_when_asked_and_never_with_import_flags(
        turso, mail, monkeypatch, capsys):
    online_world(turso)
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    assert import_online.main([]) == 0 and mail.sent == []
    start("blog")
    assert import_online.main(["--notify", "--dry-run"]) == 0
    assert "would be" in capsys.readouterr().out and mail.sent == []
    monkeypatch.setattr(import_online, "SEND_INTERVAL", 0)
    assert import_online.main(["--notify"]) == 0
    assert [to for to, _, _ in mail.sent] == ["ada@example.com"]
    for flags in (["--notify", "--link-existing", "ada@example.com"],
                  ["--start", "--link-existing", "ada@example.com"], ["--start", "--notify"]):
        with pytest.raises(SystemExit):
            import_online.main(flags)


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

    import_online.start()
    started = boot(monkeypatch)
    shop = [s["id"] for s in db.all_(
        "SELECT s.id FROM services s JOIN projects p ON p.id=s.project_id "
        "WHERE p.external_id='p-shop' ORDER BY s.id")]
    assert started == [(sid, "import") for sid in shop]
    # Reserved as admitted: --notify counts them as started, the next start does not
    # start them twice, and the RAM checks of later services count them.
    assert {s["status"] for s in db.all_("SELECT status FROM services WHERE id IN (?,?)",
                                         tuple(shop))} == {"deploying"}


def boot(monkeypatch) -> list[tuple[int, str]]:
    """main.startup with Docker, the watchdog and the event bus out of the way."""
    started = []

    async def fake_deploy(service_id, trigger="manual"):
        started.append((service_id, trigger))
        # what engine._deploy_sync does first
        db.q("INSERT INTO deployments(service_id,trigger,status,created_at) "
             "VALUES(?,?,'success',?)", (service_id, trigger, db.now()))

    async def idle():
        return None

    monkeypatch.setattr(main.engine, "deploy", fake_deploy)
    monkeypatch.setattr(main.engine, "dock",
                        lambda: types.SimpleNamespace(networks=types.SimpleNamespace(
                            get=lambda name: None)))
    monkeypatch.setattr(main.watchdog, "run_forever", idle)
    monkeypatch.setattr(main.watchdog, "attach_loop", lambda loop: None)
    monkeypatch.setattr(main.bus, "attach_loop", lambda loop: None)
    monkeypatch.setattr(main.bus, "_loop", None)
    monkeypatch.setattr(metrics, "ensure_fresh", lambda: None)

    async def go():
        await main.startup()
        await asyncio.sleep(0)

    asyncio.run(go())
    return started


def three_service_world(t: FakeTurso):
    t.user("u-a", "a@example.com")
    t.github("u-a", 1, "a")
    t.project("p", "u-a", "shop", "shop", [("shop", "shop", "a/shop", "main"),
                                           ("api", "shop-api", "a/api", "main"),
                                           ("worker", "shop-worker", "a/worker", "main")])


def test_startup_leaves_what_does_not_fit_the_quota_waiting_and_says_why(
        turso, monkeypatch, capsys):
    """The RAM quota runs 2 services at 768MB: all three at once would fail the third
    in the pipeline, mark it failed and email its owner, with no retry."""
    three_service_world(turso)
    run()
    import_online.start()
    started = boot(monkeypatch)
    ids = [s["id"] for s in db.all_("SELECT id FROM services ORDER BY id")]
    assert started == [(ids[0], "import"), (ids[1], "import")]
    worker = db.one("SELECT * FROM services WHERE id=?", (ids[2],))
    assert worker["status"] == "new"
    assert count("deployments") == 2
    assert "import: shop-worker waits: RAM quota exceeded" in capsys.readouterr().out
    # The admin raises the quota; the next start picks it up, and nothing twice.
    db.q("UPDATE users SET quota_ram_mb=4096 WHERE email='a@example.com'")
    assert boot(monkeypatch) == [(ids[2], "import")]


def test_startup_respects_the_instance_limits(turso, monkeypatch, capsys):
    online_world(turso)
    run()
    import_online.start()
    metrics.set_global_limits(2, 10, 6144)                    # the box holds 3 services
    assert boot(monkeypatch) == []
    assert "Instance-wide service limit reached" in capsys.readouterr().out
    assert {s["status"] for s in db.all_("SELECT status FROM services")} == {"new"}


def test_startup_holds_back_an_owner_without_a_github_connection(turso, monkeypatch, capsys):
    online_world(turso)
    run()
    owner = box_user("owner@example.com")["id"]
    db.q("DELETE FROM github_connections WHERE user_id=?", (owner,))
    import_online.start()
    started = boot(monkeypatch)
    blog = db.one("SELECT id FROM services WHERE slug='blog'")["id"]
    assert started == [(blog, "import")]
    assert "import: shop waits: its owner has no GitHub connection here" in capsys.readouterr().out


def test_a_push_never_deploys_an_imported_service_before_startup_has(turso, client, monkeypatch):
    online_world(turso)
    run()
    deployed = []

    async def fake_deploy(service_id, trigger="manual"):
        deployed.append(service_id)

    monkeypatch.setattr(main.engine, "deploy", fake_deploy)
    monkeypatch.setattr(main.bus, "_loop", None)
    body = b'{"ref":"refs/heads/main","after":"abc","repository":{"full_name":"ada/blog"}}'
    signed = {"X-GitHub-Event": "push", "X-Hub-Signature-256": "sha256=" + hmac.new(
        b"hook-secret", body, hashlib.sha256).hexdigest()}            # the imported App's
    r = client.post("/api/webhooks/github", content=body, headers=signed)
    assert r.json()["deployed"] == []
    blog = db.one("SELECT id FROM services WHERE slug='blog'")["id"]
    db.q("INSERT INTO deployments(service_id,status,created_at) VALUES(?,'success',?)",
         (blog, db.now()))
    r = client.post("/api/webhooks/github", content=body, headers=signed)
    assert r.json()["deployed"] == ["blog"]


# ---------- sign-ups wait for the import ----------

def test_sign_ups_wait_while_the_online_workspace_is_still_to_come(turso, client, monkeypatch):
    monkeypatch.setenv("TURSO_DATABASE_URL", ENV["TURSO_DATABASE_URL"])
    page = client.get("/signup")
    assert "Sign-ups open shortly" in page.text and 'action="/signup"' not in page.text
    r = client.post("/signup", data={"email": "early@example.com", "password": "long enough"},
                    follow_redirects=False)
    assert r.status_code == 200 and box_user("early@example.com") is None
    turso.user("u-ada", "ada@example.com")
    run(dry_run=True)                                         # a dry run commits nothing
    assert "Sign-ups open shortly" in client.get("/signup").text
    run()
    assert 'action="/signup"' in client.get("/signup").text
    r = client.post("/signup", data={"email": "late@example.com", "password": "long enough"},
                    follow_redirects=False)
    assert r.status_code == 303 and box_user("late@example.com") is not None


def test_without_an_online_workspace_sign_ups_are_open(client, monkeypatch):
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    assert 'action="/signup"' in client.get("/signup").text


# ---------- --link-existing names the addresses it merges ----------

def test_link_existing_merges_only_the_addresses_it_names(turso, client, monkeypatch):
    """The review's PoC: before sign-ups were closed from the first start, a squatter
    could sign up here as the victim (email off: confirmed on the spot) and sit in the
    same "already has an account here" list as the operator's own account. A bare
    --link-existing, meant for the operator's, merged the squatter's too."""
    monkeypatch.setattr(mailer, "RESEND_API_KEY", "")
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    r = client.post("/signup", data={"email": "victim@example.com", "password": "attacker-pw"},
                    follow_redirects=False)
    assert r.status_code == 303 and box_user("victim@example.com")["email_verified"] == 1
    mine = box_account("ada@example.com")                    # the operator's, legitimately
    ada_online(turso)
    turso.user("u-victim", "victim@example.com")
    turso.github("u-victim", 8888, "victim")
    turso.project("p-v", "u-victim", "vproj", "vproj", [("vproj", "vproj", "victim/secret", "main")])
    report = run(link_existing="ada@example.com")
    assert report.users_linked == ["ada@example.com"]
    assert [e.split(":")[0] for e in report.users_existing] == ["victim@example.com"]
    assert db.one("SELECT 1 FROM github_connections WHERE installation_id=8888") is None
    assert db.one("SELECT user_id FROM github_connections WHERE installation_id=7777")[
        "user_id"] == mine
    assert "--link-existing and its address" in report.summary()


def test_link_existing_says_when_an_address_it_names_had_nobody_to_link(turso):
    box_account("ada@example.com")
    ada_online(turso)
    report = run(link_existing="ada@example.com, nobody@example.com")
    assert report.users_linked == ["ada@example.com"]
    assert report.link_unused == ["nobody@example.com"]
    assert "--link-existing names nobody@example.com, but no online account" in report.summary()


def test_link_existing_takes_addresses_not_a_yes(turso, monkeypatch, capsys):
    ada_online(turso)
    with pytest.raises(TypeError):
        run(link_existing=True)
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    assert import_online.main(["--link-existing", "ada"]) == 1
    assert "takes email addresses" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        import_online.main(["--link-existing"])                 # it needs the addresses


# ---------- a GitHub disconnect online is carried over ----------

def test_a_github_disconnect_online_since_is_carried_over(turso, monkeypatch):
    """The review's PoC: Disconnect online deletes the cx_github row and leaves the
    projects. A re-run left the box's binding, so --start and a restart cloned and
    deployed the blog through the installation its owner had disconnected."""
    ada_online(turso)
    run()
    turso.db.execute("DELETE FROM cx_github WHERE userId='u-ada'")          # Disconnect online
    report = run()
    assert db.one("SELECT 1 FROM github_connections WHERE installation_id=7777") is None
    assert report.connections_removed == [
        "ada@example.com: installation 7777 was disconnected online since; removed here"]
    assert report.held_back == ["blog (blog) of ada@example.com: no GitHub connection here"]
    assert "installation 7777 was disconnected online" in report.summary()
    import_online.start()
    assert boot(monkeypatch) == []


def test_another_installation_connected_online_since_replaces_the_imported_one(turso):
    ada_online(turso)
    run()
    turso.db.execute("UPDATE cx_github SET installationId=7778 WHERE userId='u-ada'")
    report = run()
    rows = db.all_("SELECT installation_id, external_id FROM github_connections")
    assert [(r["installation_id"], r["external_id"]) for r in rows] == [(7778, "u-ada")]
    assert report.connections_new == 1 and len(report.connections_removed) == 1


def test_a_connection_made_here_is_never_removed_by_a_re_run(turso):
    """Only what the import brought over follows the online workspace: a connection
    its owner made on the box is theirs, whatever the online table says."""
    ada_online(turso)
    turso.db.execute("DELETE FROM cx_github")
    run()
    ada = box_user("ada@example.com")["id"]
    db.q("INSERT INTO github_connections(user_id,kind,installation_id,created_at) "
         "VALUES(?,'app',7777,?)", (ada, db.now()))                       # Connect, here
    report = run()
    assert db.one("SELECT 1 FROM github_connections WHERE installation_id=7777")
    assert report.connections_removed == []


def test_without_the_online_github_table_no_connection_is_removed(turso, monkeypatch):
    ada_online(turso)
    run()
    real = import_online.fetch

    def without_github(base, token):
        data = real(base, token)
        data["tables"] = [t for t in data["tables"] if t["name"] != "cx_github"]
        data["github"] = []
        return data

    monkeypatch.setattr(import_online, "fetch", without_github)
    assert run().connections_removed == []
    assert db.one("SELECT 1 FROM github_connections WHERE installation_id=7777")


# ---------- nothing imported deploys before --start ----------

def test_no_restart_deploys_anything_imported_before_start(turso, monkeypatch, capsys):
    """RUNBOOK step 4's remedies restart cx-control (`docker compose up -d` after a fix
    to .env), and step 6 may restart Traefik: a customer app deployed by then would run
    before app.<domain> is here, and be dropped. Only the operator's --start opens it."""
    online_world(turso)
    report = run()
    assert "nothing deploys yet" in report.summary() and "--start" in report.summary()
    assert boot(monkeypatch) == [] and boot(monkeypatch) == []
    assert "import: 3 imported service(s) wait for `python -m app.import_online --start`" \
        in capsys.readouterr().out
    assert {s["status"] for s in db.all_("SELECT status FROM services")} == {"new"}
    assert count("deployments") == 0
    started = import_online.start()
    assert started.waiting == 3 and "Recorded" in started.summary()
    assert len(boot(monkeypatch)) == 3
    assert "--start has run here" in run().summary()


def test_start_needs_an_import_and_records_nothing_on_a_dry_run(turso):
    with pytest.raises(import_online.Refused, match="nothing has been imported here yet"):
        import_online.start()
    online_world(turso)
    run()
    assert "DRY RUN" in import_online.start(dry_run=True).summary()
    assert db.setting(import_online.START_KEY) is None
    first = import_online.start()
    again = import_online.start()
    assert not first.since and again.since and "already ran" in again.summary()


def test_the_start_command_prints_what_waits_or_refuses_with_exit_1(turso, capsys):
    assert import_online.main(["--start"]) == 1
    assert "start refused, nothing was recorded" in capsys.readouterr().err
    three_service_world(turso)
    run()
    assert import_online.main(["--start"]) == 0
    out = capsys.readouterr().out
    assert "3 imported service(s) wait to deploy" in out and "docker restart cx-control" in out
    assert "a@example.com: 3 to deploy, their RAM quota" in out


# ---------- /admin prints the RUNBOOK's steps ----------

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))


def test_admin_prints_the_runbooks_steps_in_its_order():
    """An operator may follow /admin's printed steps instead of docs/RUNBOOK.md. Without
    the second import after the DNS move, every sign-up, reset and project made online
    between the two was lost; without --start, the restart deployed nothing."""
    import re
    page = open(os.path.join(REPO, "site", "admin.html"), encoding="utf-8").read()
    term = re.search(r'<pre class="term">(.*?)</pre>', page, re.S).group(1)
    lines = [ln.strip() for ln in re.sub(r"<[^>]+>", "", term).splitlines() if ln.strip()]
    printed = [ln for ln in lines if not ln.startswith("#")]
    imp = "docker exec cx-control python -m app.import_online"
    assert printed == [f"{imp} --dry-run", imp, imp, "VERIFY_ALLOW_RESTART=1 ./verify-public.sh",
                       f"{imp} --start", "docker restart cx-control", f"{imp} --notify"]
    dns = next(i for i, ln in enumerate(lines) if "Cloudflare DNS" in ln)
    imports = [i for i, ln in enumerate(lines) if ln == imp]
    assert imports[0] < dns < imports[1]
    # The same commands, in the same order, as the RUNBOOK's section.
    runbook = open(os.path.join(REPO, "docs", "RUNBOOK.md"), encoding="utf-8").read()
    section = runbook.split("## Leaving Vercel", 1)[1].split("\n## ", 1)[0]
    commands = [re.sub(r" && docker logs -f cx-control$", "",
                       ln.strip().removeprefix("cd /root/cicatrixa-platform && "))
                for block in re.findall(r"```bash\n(.*?)```", section, re.S)
                for ln in block.splitlines()]
    at = -1
    for c in printed:                       # each one after the one before (raises if not)
        at = commands.index(c, at + 1)
