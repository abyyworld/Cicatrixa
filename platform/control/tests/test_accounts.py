"""Signing up, signing in, and getting back in after forgetting the password —
through the real routes, against a real database, with email stubbed out."""
import time

import pytest
from fastapi.testclient import TestClient

from app import auth, billing, bus, db, engine, gh, main, mailer, metrics, promote


@pytest.fixture
def client(fresh_db, monkeypatch):
    for state in (main._reset_sent_at, main._code_sent_at, main._code_failures,
                  main._applying):
        state.clear()
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
    assert r.status_code == 303 and r.headers["location"] == "/dashboard"


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


def admins():
    return {r["email"] for r in db.all_("SELECT email FROM users WHERE is_admin=1")}


def test_with_email_off_typing_the_admin_address_makes_nobody_admin(client, monkeypatch):
    """Anyone can type the owner's address, and with email off nothing checks it.
    It used to be enough — and an admin can mint a reset link for any account."""
    monkeypatch.setattr(main, "ADMIN_EMAILS", {"owner@example.com"})
    client.post("/signup", data={"email": "owner@example.com", "password": "long enough"})
    client.post("/signup", data={"email": "stranger@example.com", "password": "long enough"})
    assert admins() == set()


def test_the_promote_command_is_how_the_owner_becomes_admin_with_email_off(client, monkeypatch):
    monkeypatch.setattr(main, "ADMIN_EMAILS", {"owner@example.com"})
    client.post("/signup", data={"email": "owner@example.com", "password": "long enough"})
    assert promote.main(["promote", " Owner@Example.com "]) == 0
    assert admins() == {"owner@example.com"}
    assert promote.main(["promote", "nobody@example.com"]) == 1
    assert promote.main(["promote"]) == 2


def test_with_email_on_the_admin_address_becomes_admin_once_its_code_is_entered(
        client, outbox, monkeypatch):
    monkeypatch.setattr(main, "ADMIN_EMAILS", {"owner@example.com"})
    client.post("/signup", data={"email": "owner@example.com", "password": "long enough"})
    assert admins() == set()
    [(_, _, code)] = outbox
    client.post("/verify-code", data={"code": code})
    assert admins() == {"owner@example.com"}


def test_a_squatter_cannot_guess_the_admin_addresss_code(client, outbox, monkeypatch):
    monkeypatch.setattr(main, "ADMIN_EMAILS", {"owner@example.com"})
    client.post("/signup", data={"email": "owner@example.com", "password": "long enough"})
    [(_, _, code)] = outbox
    wrong = f"{(int(code) + 1) % 1000000:06d}"
    for _ in range(main.CODE_ATTEMPTS - 1):
        assert "incorrect" in client.post("/verify-code", data={"code": wrong}).text
    assert "Too many wrong codes" in client.post("/verify-code", data={"code": wrong}).text
    # The code is dead now: even the right one no longer works.
    r = client.post("/verify-code", data={"code": code}, follow_redirects=False)
    assert r.status_code == 200 and admins() == set()


# ---------- code emails ----------

def test_resending_within_a_minute_sends_nothing(client, outbox):
    client.post("/signup", data={"email": "new@example.com", "password": "long enough"})
    r = client.post("/verify-code/resend", follow_redirects=False)
    assert r.headers["location"] == "/verify-code?wait=1"
    assert len(outbox) == 1
    assert "less than a minute ago" in client.get("/verify-code?wait=1").text


def test_logging_in_again_and_again_does_not_flood_the_inbox(client, outbox):
    client.post("/signup", data={"email": "new@example.com", "password": "long enough"})
    for _ in range(3):
        client.post("/verify-code/cancel")
        log_in(client, "new@example.com", "long enough")
    assert len(outbox) == 1


def test_after_too_many_wrong_codes_resend_works_at_once(client, outbox):
    client.post("/signup", data={"email": "new@example.com", "password": "long enough"})
    for _ in range(main.CODE_ATTEMPTS):
        client.post("/verify-code", data={"code": "000000" if outbox[0][2] != "000000"
                                          else "111111"})
    r = client.post("/verify-code/resend", follow_redirects=False)
    assert r.headers["location"] == "/verify-code?resent=1" and len(outbox) == 2


# ---------- sessions ----------

def test_a_password_reset_ends_every_other_session(client):
    uid = make_user()
    elsewhere = auth.make_session(uid)
    assert auth.session_user_id(elsewhere) == uid
    client.post("/reset-password", data={"token": auth.make_reset(user(uid)),
                                         "password": "brand new pw"},
                follow_redirects=False)
    assert auth.session_user_id(elsewhere) is None
    client.cookies.set(auth.COOKIE_NAME, elsewhere)
    assert client.get("/dashboard", follow_redirects=False).headers["location"] \
        .startswith("/login")


def test_the_github_install_state_is_not_a_login(fresh_db):
    uid = make_user()
    assert auth.session_user_id(auth.make_gh_state(uid)) is None
    assert auth.gh_state_user_id(auth.make_session(uid)) is None
    assert auth.gh_state_user_id(auth.make_gh_state(uid)) == uid


# ---------- where people are sent ----------

def test_a_signed_in_non_admin_on_admin_goes_to_the_dashboard_not_round_in_circles(client):
    uid = make_user()
    client.cookies.set(auth.COOKIE_NAME, auth.make_session(uid))
    r = client.get("/admin", follow_redirects=False)
    assert r.headers["location"] == "/dashboard"


def test_a_logged_out_page_visit_comes_back_after_login(client):
    r = client.get("/projects/new", follow_redirects=False)
    assert r.headers["location"] == "/login?next=%2Fprojects%2Fnew"


def test_a_logged_out_form_post_is_not_replayed_as_a_get(client):
    """A POST-only URL as next would end on a 405 after login."""
    r = client.post("/projects/1/deploy", follow_redirects=False)
    assert r.headers["location"] == "/login"


# ---------- GitHub installations ----------

def bind(uid, installation_id):
    db.q("INSERT INTO github_connections(user_id,kind,installation_id,gh_login,created_at)"
         " VALUES(?,?,?,?,?)", (uid, "app", installation_id, "", db.now()))


def connections(installation_id):
    return [r["user_id"] for r in db.all_(
        "SELECT user_id FROM github_connections WHERE installation_id=?",
        (installation_id,))]


def test_someone_elses_installation_cannot_be_claimed(client):
    victim, thief = make_user("victim@example.com"), make_user("thief@example.com")
    bind(victim, 4242)
    client.cookies.set(auth.COOKIE_NAME, auth.make_session(thief))
    r = client.get("/connect/github/setup?installation_id=4242", follow_redirects=False)
    assert r.headers["location"].startswith("/connect/github?error=")
    assert connections(4242) == [victim]


def test_a_new_installation_needs_githubs_word_that_it_is_yours(client, monkeypatch):
    thief = make_user("thief@example.com")
    client.cookies.set(auth.COOKIE_NAME, auth.make_session(thief))
    db.set_setting("gh_app_oauth_on_install", "1")
    r = client.get("/connect/github/setup?installation_id=777", follow_redirects=False)
    assert "error=" in r.headers["location"] and connections(777) == []

    monkeypatch.setattr(gh, "user_installation_ids", lambda code: {1, 2, 3})
    r = client.get("/connect/github/callback?installation_id=777&code=abc",
                   follow_redirects=False)
    assert "error=" in r.headers["location"] and connections(777) == []


def test_an_installation_github_confirms_is_connected(client, monkeypatch):
    uid = make_user()
    db.set_setting("gh_app_oauth_on_install", "1")
    monkeypatch.setattr(gh, "user_installation_ids", lambda code: {777})
    state = auth.make_gh_state(uid)
    r = client.get(f"/connect/github/callback?installation_id=777&code=abc&state={state}",
                   follow_redirects=False)
    assert r.headers["location"] == "/projects/new" and connections(777) == [uid]


def test_the_manifest_asks_github_for_authorization_on_install(fresh_db):
    m = gh.build_manifest("https://app.example.com")
    assert m["request_oauth_on_install"] is True
    assert "setup_url" not in m       # GitHub allows no setup_url alongside it
    assert m["callback_urls"] == ["https://app.example.com/connect/github/callback"]


# ---------- the medic's Apply button ----------

def test_apply_answers_at_once_and_runs_once(client, monkeypatch):
    calls = []

    async def fake_apply(project_id, message_id):
        calls.append(message_id)
    monkeypatch.setattr(main, "_apply_fix", fake_apply)
    uid = make_user()
    pid = db.q("INSERT INTO projects(user_id,name,slug,created_at) VALUES(?,?,?,?)",
               (uid, "p", "p", db.now())).lastrowid
    client.cookies.set(auth.COOKIE_NAME, auth.make_session(uid))
    for _ in range(2):
        r = client.post(f"/projects/{pid}/chat/9/apply", follow_redirects=False)
        assert r.headers["location"] == f"/projects/{pid}"
    assert calls == [9]


# ---------- billing ----------

def test_a_first_payment_counts_once_whatever_order_stripe_sends_it(fresh_db):
    import json
    uid = make_user()
    db.q("UPDATE users SET stripe_customer_id='cus_1' WHERE id=?", (uid,))
    checkout = json.dumps({"type": "checkout.session.completed", "data": {"object": {
        "client_reference_id": str(uid), "customer": "cus_1", "invoice": "in_1"}}}).encode()
    paid = json.dumps({"type": "invoice.paid", "data": {"object": {
        "id": "in_1", "customer": "cus_1"}}}).encode()
    for event in (paid, checkout, paid, checkout):   # out of order, and retried
        billing.handle_event(event)
    days = (user(uid)["paid_until"] - db.now()) / 86400
    assert billing.PAID_CYCLE_DAYS - 1 < days <= billing.PAID_CYCLE_DAYS

    renewal = json.dumps({"type": "invoice.paid", "data": {"object": {
        "id": "in_2", "customer": "cus_1"}}}).encode()
    billing.handle_event(renewal)
    days = (user(uid)["paid_until"] - db.now()) / 86400
    assert 2 * billing.PAID_CYCLE_DAYS - 1 < days <= 2 * billing.PAID_CYCLE_DAYS


# ---------- the test runner's volume ----------

def test_test_containers_mount_the_volume_actually_behind_data(monkeypatch):
    class Me:
        attrs = {"Mounts": [{"Type": "bind", "Destination": "/var/run/docker.sock"},
                            {"Type": "volume", "Destination": "/data",
                             "Name": "cicatrixa-platform_cx-data"}]}

    class Containers:
        def get(self, _):
            return Me()

    class Dock:
        containers = Containers()
    monkeypatch.setattr(engine, "DATA_VOLUME", "")
    monkeypatch.setattr(engine, "dock", lambda: Dock())
    assert engine.data_volume() == "cicatrixa-platform_cx-data"


def test_an_explicit_data_volume_wins(monkeypatch):
    monkeypatch.setattr(engine, "DATA_VOLUME", "custom")
    assert engine.data_volume() == "custom"


# ---------- live logs ----------

def test_the_event_stream_starts_at_once_and_never_goes_quiet(monkeypatch):
    import asyncio
    monkeypatch.setattr(bus, "HEARTBEAT", 0.01)

    async def first_two():
        stream = bus.subscribe("project:test")
        return [await stream.__anext__(), await stream.__anext__()]
    assert asyncio.run(first_two()) == [": connected\n\n", ": keep-alive\n\n"]
