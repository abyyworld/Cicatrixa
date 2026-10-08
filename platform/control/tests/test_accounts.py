"""Signing up, signing in, and getting back in after forgetting the password —
through the real routes, against a real database, with email stubbed out."""
import time

import pytest
from fastapi.testclient import TestClient

from app import auth, db, main, mailer, metrics


@pytest.fixture
def client(fresh_db, monkeypatch):
    main._reset_sent_at.clear()
    monkeypatch.setattr(metrics, "ensure_fresh", lambda: None)
    # No `with`: the startup hook talks to Docker, and none of this needs it.
    return TestClient(main.app)


@pytest.fixture
def outbox(monkeypatch):
    """Turns email on and records what would have been sent."""
    sent = []
    monkeypatch.setattr(mailer, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(mailer, "send_verification_code",
                        lambda to, code: sent.append(("code", to, code)) or True)
    monkeypatch.setattr(mailer, "send_password_reset",
                        lambda to, url: sent.append(("reset", to, url)) or True)
    return sent


def make_user(email="ada@example.com", password="correct horse", verified=1, admin=0):
    return db.q("INSERT INTO users(email,pw_hash,email_verified,is_admin,created_at) "
                "VALUES(?,?,?,?,?)", (email, auth.hash_password(password), verified,
                                      admin, db.now())).lastrowid


def user(uid):
    return db.one("SELECT * FROM users WHERE id=?", (uid,))


def log_in(client, email, password, **extra):
    return client.post("/login", data={"email": email, "password": password, **extra},
                       follow_redirects=False)


# ---------- the reset token ----------

def test_a_reset_link_names_its_user(fresh_db):
    uid = make_user()
    assert auth.reset_user(auth.make_reset(user(uid)))["id"] == uid


def test_a_reset_link_dies_when_the_password_changes(fresh_db):
    uid = make_user()
    token = auth.make_reset(user(uid))
    db.q("UPDATE users SET pw_hash=? WHERE id=?", (auth.hash_password("new one!"), uid))
    assert auth.reset_user(token) is None


def test_a_tampered_reset_link_is_refused(fresh_db):
    uid = make_user()
    other = make_user("eve@example.com")
    token = auth.make_reset(user(uid))
    assert auth.reset_user(token.replace(f"reset:{uid}:", f"reset:{other}:", 1)) is None


def test_an_expired_reset_link_is_refused(fresh_db, monkeypatch):
    uid = make_user()
    token = auth.make_reset(user(uid))
    monkeypatch.setattr(time, "time", lambda: 10 ** 11)
    assert auth.reset_user(token) is None


def test_reset_links_sessions_and_pending_tokens_are_not_interchangeable(fresh_db):
    uid = make_user()
    assert auth.reset_user(auth.make_session(uid)) is None
    assert auth.reset_user(auth.make_pending(uid)) is None
    assert auth.session_user_id(auth.make_reset(user(uid))) is None
    assert auth.pending_user_id(auth.make_reset(user(uid))) is None


# ---------- forgot password ----------

def test_forgot_password_emails_a_link_that_works(client, outbox):
    uid = make_user()
    r = client.post("/forgot-password", data={"email": " Ada@Example.com "})
    assert r.status_code == 200 and "on its way" in r.text
    [(kind, to, url)] = outbox
    assert (kind, to) == ("reset", "ada@example.com")
    assert url.startswith(f"{main.BASE_URL}/reset-password?token=")

    path = url[len(main.BASE_URL):]
    assert "ada@example.com" in client.get(path).text
    r = client.post("/reset-password",
                    data={"token": path.split("token=", 1)[1].replace("%3A", ":"),
                          "password": "brand new pw"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard?password_reset=1"
    assert auth.session_user_id(r.cookies.get(auth.COOKIE_NAME)) == uid

    assert log_in(client, "ada@example.com", "brand new pw").status_code == 303
    assert "Wrong email or password" in log_in(client, "ada@example.com", "correct horse").text


def test_a_used_reset_link_does_not_work_twice(client):
    uid = make_user()
    token = auth.make_reset(user(uid))
    client.post("/reset-password", data={"token": token, "password": "first new"},
                follow_redirects=False)
    r = client.post("/reset-password", data={"token": token, "password": "second new"},
                    follow_redirects=False)
    assert r.status_code == 200 and "expired" in r.text
    assert auth.verify_password("first new", user(uid)["pw_hash"])


def test_a_short_new_password_is_refused_and_the_old_one_kept(client):
    uid = make_user()
    r = client.post("/reset-password",
                    data={"token": auth.make_reset(user(uid)), "password": "short"},
                    follow_redirects=False)
    assert r.status_code == 200 and "at least 8" in r.text
    assert auth.verify_password("correct horse", user(uid)["pw_hash"])


def test_resetting_the_password_also_verifies_the_email(client):
    uid = make_user(verified=0)
    client.post("/reset-password",
                data={"token": auth.make_reset(user(uid)), "password": "brand new pw"},
                follow_redirects=False)
    assert user(uid)["email_verified"] == 1


def test_forgot_password_answers_the_same_for_an_unknown_email(client, outbox):
    r = client.post("/forgot-password", data={"email": "nobody@example.com"})
    assert r.status_code == 200 and "on its way" in r.text
    assert outbox == []


def test_forgot_password_cannot_flood_an_inbox(client, outbox):
    make_user()
    for _ in range(3):
        client.post("/forgot-password", data={"email": "ada@example.com"})
    assert len(outbox) == 1


def test_forgot_password_without_email_points_at_a_human(client):
    make_user()
    r = client.post("/forgot-password", data={"email": "ada@example.com"})
    assert "can't send email" in r.text


def test_the_login_page_links_to_forgot_password(client):
    assert 'href="/forgot-password"' in client.get("/login").text


# ---------- the admin's hand-delivered link ----------

def test_an_admin_can_make_a_reset_link(client):
    admin = make_user("boss@example.com", admin=1)
    uid = make_user()
    client.cookies.set(auth.COOKIE_NAME, auth.make_session(admin))
    r = client.post(f"/admin/users/{uid}/reset-link")
    assert r.status_code == 200 and "Reset link for ada@example.com" in r.text
    token = r.text.split("/reset-password?token=", 1)[1].split('"', 1)[0]
    assert auth.reset_user(token.replace("%3A", ":"))["id"] == uid


def test_only_an_admin_can_make_a_reset_link(client):
    uid = make_user()
    client.cookies.set(auth.COOKIE_NAME, auth.make_session(uid))
    r = client.post(f"/admin/users/{uid}/reset-link", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


# ---------- signing up ----------

def test_signup_without_email_configured_goes_straight_in(client):
    r = client.post("/signup", data={"email": "new@example.com", "password": "long enough"},
                    follow_redirects=False)
    assert r.headers["location"] == "/dashboard"
    uid = auth.session_user_id(r.cookies.get(auth.COOKIE_NAME))
    assert user(uid)["email_verified"] == 1


def test_signup_with_email_sends_a_code_and_waits_for_it(client, outbox):
    r = client.post("/signup", data={"email": "new@example.com", "password": "long enough"},
                    follow_redirects=False)
    assert r.headers["location"] == "/verify-code"
    [(kind, to, code)] = outbox
    assert (kind, to) == ("code", "new@example.com")
    r = client.post("/verify-code", data={"code": code}, follow_redirects=False)
    assert r.headers["location"] == "/dashboard?verified=1"


def test_a_code_email_that_fails_is_admitted_not_hidden(client, monkeypatch):
    """It used to be fired off in the background and forgotten: the page said a
    code was coming when none ever would."""
    monkeypatch.setattr(mailer, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(mailer, "send_verification_code", lambda to, code: False)
    r = client.post("/signup", data={"email": "new@example.com", "password": "long enough"},
                    follow_redirects=False)
    assert r.headers["location"] == "/verify-code?unsent=1"
    page = client.get(r.headers["location"]).text
    assert "couldn't send the email" in page and "We sent a 6-digit code" not in page


def test_a_failed_send_is_reported_to_admins(monkeypatch):
    class Refused:
        status_code = 403
        text = '{"message":"The cicatrixa.com domain is not verified."}'
    monkeypatch.setattr(mailer, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(mailer, "last_error", "")
    monkeypatch.setattr(mailer.httpx, "post", lambda *a, **k: Refused())
    assert mailer.send("a@example.com", "s", "t") is False
    assert "403" in mailer.last_error and "not verified" in mailer.last_error


# ---------- signing in ----------

def test_login_returns_to_the_page_that_asked_for_it(client):
    make_user()
    r = log_in(client, "ada@example.com", "correct horse", next="/projects/new")
    assert r.headers["location"] == "/projects/new"


@pytest.mark.parametrize("target", ["https://evil.example", "//evil.example",
                                    "/\\evil.example", "javascript:alert(1)"])
def test_login_never_sends_anyone_off_site(client, target):
    make_user()
    r = log_in(client, "ada@example.com", "correct horse", next=target)
    assert r.headers["location"] == "/dashboard"


def test_a_wrong_password_is_refused(client):
    make_user()
    r = log_in(client, "ada@example.com", "not it")
    assert r.status_code == 200 and "Wrong email or password" in r.text


# ---------- who becomes an admin ----------

def test_without_admin_emails_the_first_account_is_the_admin(client, monkeypatch):
    monkeypatch.setattr(main, "ADMIN_EMAILS", set())
    client.post("/signup", data={"email": "first@example.com", "password": "long enough"})
    assert db.one("SELECT is_admin FROM users WHERE email='first@example.com'")["is_admin"] == 1


def test_with_admin_emails_a_stranger_signing_up_first_is_not_an_admin(client, monkeypatch):
    monkeypatch.setattr(main, "ADMIN_EMAILS", {"owner@example.com"})
    client.post("/signup", data={"email": "stranger@example.com", "password": "long enough"})
    client.post("/signup", data={"email": "owner@example.com", "password": "long enough"})
    admins = {r["email"] for r in db.all_("SELECT email FROM users WHERE is_admin=1")}
    assert admins == {"owner@example.com"}
