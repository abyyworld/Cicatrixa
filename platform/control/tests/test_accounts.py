"""Signing up, signing in, and getting back in after forgetting the password —
through the real routes, against a real database, with email stubbed out."""
import time

import pytest
from fastapi.testclient import TestClient

from app import auth, billing, bus, db, engine, gh, main, mailer, metrics, promote


@pytest.fixture
def client(fresh_db, monkeypatch):
    for state in (main._reset_sent_at, main._code_issued_at, main._code_failures,
                  main._code_strikes, main._applying):
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


def test_promote_makes_the_admin_account_with_a_password_only_the_shell_holder_sets(
        client, monkeypatch):
    monkeypatch.setattr(promote.db, "DB_PATH", db.DB_PATH)
    link = promote.promote(" Owner@Example.com ")
    assert admins() == {"owner@example.com"}
    token = link.split("token=", 1)[1].replace("%3A", ":")
    r = client.post("/reset-password", data={"token": token, "password": "owners own pw"},
                    follow_redirects=False)
    assert r.headers["location"] == "/dashboard?password_reset=1"
    assert log_in(client, "owner@example.com", "owners own pw").status_code == 303
    assert promote.main(["promote"]) == 2


def test_promote_takes_the_address_back_from_whoever_registered_it_first(client, monkeypatch):
    """With email off a squatter may have signed up as the owner first. Promoting
    that account must not hand the squatter admin: their password and sessions go."""
    monkeypatch.setattr(main, "ADMIN_EMAILS", {"owner@example.com"})
    client.post("/signup", data={"email": "owner@example.com", "password": "squatters pw"})
    squatter_cookie = client.cookies.get(auth.COOKIE_NAME)
    assert auth.session_user_id(squatter_cookie)
    promote.promote("owner@example.com")
    assert auth.session_user_id(squatter_cookie) is None
    assert "Wrong email or password" in log_in(client, "owner@example.com",
                                               "squatters pw").text


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
    assert "once a minute" in client.get("/verify-code?wait=1").text


def test_logging_in_again_and_again_does_not_flood_the_inbox(client, outbox):
    client.post("/signup", data={"email": "new@example.com", "password": "long enough"})
    for _ in range(3):
        client.post("/verify-code/cancel")
        log_in(client, "new@example.com", "long enough")
    assert len(outbox) == 1


def wrong(code):
    return "000000" if code != "000000" else "111111"


def test_killing_a_code_does_not_buy_a_new_one_sooner(client, outbox, monkeypatch):
    """5 wrong guesses, then Resend, then 5 more, round and round, was a guess
    every few milliseconds: the cooldown has to hold across a dead code."""
    client.post("/signup", data={"email": "new@example.com", "password": "long enough"})
    for _ in range(main.CODE_ATTEMPTS):
        client.post("/verify-code", data={"code": wrong(outbox[0][2])})
    r = client.post("/verify-code/resend", follow_redirects=False)
    assert r.headers["location"] == "/verify-code?wait=1" and len(outbox) == 1
    later = time.time() + main.CODE_COOLDOWN + 1
    monkeypatch.setattr(main.time, "time", lambda: later)
    r = client.post("/verify-code/resend", follow_redirects=False)
    assert r.headers["location"] == "/verify-code?resent=1" and len(outbox) == 2


def test_a_failed_send_still_counts_toward_the_cooldown(client, monkeypatch):
    monkeypatch.setattr(mailer, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(mailer, "send_verification_code", lambda to, code: False)
    client.post("/signup", data={"email": "new@example.com", "password": "long enough"})
    r = client.post("/verify-code/resend", follow_redirects=False)
    assert r.headers["location"] == "/verify-code?wait=1"


def test_an_hour_of_wrong_guesses_is_capped_across_codes(client, outbox, monkeypatch):
    client.post("/signup", data={"email": "new@example.com", "password": "long enough"})
    clock = [time.time()]
    monkeypatch.setattr(main.time, "time", lambda: clock[0])
    for _ in range(main.CODE_STRIKES // main.CODE_ATTEMPTS):
        for _ in range(main.CODE_ATTEMPTS):
            client.post("/verify-code", data={"code": wrong(outbox[-1][2])})
        clock[0] += main.CODE_COOLDOWN + 1
        client.post("/verify-code/resend")
    r = client.post("/verify-code/resend", follow_redirects=False)
    assert r.headers["location"] == "/verify-code?blocked=1"
    assert "Too many wrong codes" in client.get("/verify-code?blocked=1").text
    clock[0] += 3601          # the pending cookie (15 min) is long gone: log in again
    sent = len(outbox)
    r = log_in(client, "new@example.com", "long enough")
    assert r.headers["location"] == "/verify-code" and len(outbox) == sent + 1


def test_a_squatters_pending_cookie_dies_when_the_owner_resets(client, outbox):
    """It used to outlive the reset and, through Resend, mint a session for the
    now-verified account without its password."""
    client.post("/signup", data={"email": "owner@example.com", "password": "squatters pw"})
    pending = client.cookies.get(auth.PENDING_COOKIE_NAME)
    owner = db.one("SELECT * FROM users WHERE email='owner@example.com'")
    client.post("/reset-password", data={"token": auth.make_reset(owner),
                                         "password": "owners own pw"},
                follow_redirects=False)
    client.cookies.clear()
    client.cookies.set(auth.PENDING_COOKIE_NAME, pending)
    assert client.post("/verify-code/resend", follow_redirects=False) \
        .headers["location"] == "/login"
    assert client.post("/verify-code", data={"code": "123456"}, follow_redirects=False) \
        .headers["location"] == "/login"


def test_a_pending_cookie_never_opens_a_verified_account(client, outbox):
    client.post("/signup", data={"email": "new@example.com", "password": "long enough"})
    pending = client.cookies.get(auth.PENDING_COOKIE_NAME)
    db.q("UPDATE users SET email_verified=1 WHERE email='new@example.com'")
    client.cookies.clear()
    client.cookies.set(auth.PENDING_COOKIE_NAME, pending)
    r = client.post("/verify-code", data={"code": outbox[0][2]}, follow_redirects=False)
    assert r.headers["location"] == "/login" and not r.cookies.get(auth.COOKIE_NAME)


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


def signed_in(client, uid):
    client.cookies.set(auth.COOKIE_NAME, auth.make_session(uid))


def callback(client, uid=None, **params):
    if uid is not None:
        params.setdefault("state", auth.make_gh_state(uid))
    return client.get("/connect/github/callback", params=params, follow_redirects=False)


def test_an_installation_github_confirms_is_yours_is_connected(client, monkeypatch):
    uid = make_user()
    monkeypatch.setattr(gh, "installations_user_controls",
                        lambda code: {777: {"account": {"login": "ada"}}})
    signed_in(client, uid)
    r = callback(client, uid, installation_id=777, code="abc")
    assert r.headers["location"] == "/projects/new" and connections(777) == [uid]


def test_seeing_an_installation_is_not_owning_it(client, monkeypatch):
    """A collaborator or plain org member can see an installation; its token
    reaches every repo in it."""
    uid = make_user()
    monkeypatch.setattr(gh, "installations_user_controls", lambda code: {1: {}})
    signed_in(client, uid)
    r = callback(client, uid, installation_id=777, code="abc")
    assert "error=" in r.headers["location"] and connections(777) == []


def test_a_new_installation_needs_githubs_authorization(client):
    uid = make_user()
    signed_in(client, uid)
    r = callback(client, uid, installation_id=777)
    assert "error=" in r.headers["location"] and connections(777) == []


def test_someone_elses_installation_cannot_be_claimed(client, monkeypatch):
    victim, thief = make_user("victim@example.com"), make_user("thief@example.com")
    bind(victim, 4242)
    monkeypatch.setattr(gh, "installations_user_controls", lambda code: {4242: {}})
    signed_in(client, thief)
    r = callback(client, thief, installation_id=4242, code="abc")
    assert "error=" in r.headers["location"] and connections(4242) == [victim]


def test_a_link_started_by_someone_else_binds_nothing(client, monkeypatch):
    """Whoever made the install link must not get the installation of the person
    who follows it — nor push theirs onto that person (a cross-site GET)."""
    victim, attacker = make_user("victim@example.com"), make_user("evil@example.com")
    monkeypatch.setattr(gh, "installations_user_controls", lambda code: {777: {}})
    signed_in(client, victim)
    r = callback(client, attacker, installation_id=777, code="abc")    # attacker's state
    assert "error=" in r.headers["location"] and connections(777) == []
    r = callback(client, installation_id=777, code="abc")              # no state at all
    assert "error=" in r.headers["location"] and connections(777) == []


def test_signed_out_callbacks_go_to_login(client):
    r = callback(client, installation_id=777, code="abc")
    assert r.headers["location"].startswith("/login")


def test_an_existing_installation_reconnects_through_authorize(client, monkeypatch):
    uid = make_user()
    db.set_setting("gh_app_client_id", "Iv1.abc")
    signed_in(client, uid)
    r = client.get("/connect/github/authorize", follow_redirects=False)
    assert r.headers["location"].startswith(
        "https://github.com/login/oauth/authorize?client_id=Iv1.abc")
    monkeypatch.setattr(gh, "installations_user_controls", lambda code: {
        5: {"created_at": "2026-01-01T00:00:00Z"}, 9: {"created_at": "2026-09-01T00:00:00Z"}})
    r = callback(client, uid, code="abc")          # GitHub sends no installation_id here
    assert r.headers["location"] == "/projects/new" and connections(9) == [uid]


def test_the_connect_page_offers_the_reconnect_path(client):
    uid = make_user()
    db.set_setting("gh_app_id", "1")
    db.set_setting("gh_app_pem", "x")
    signed_in(client, uid)
    assert 'href="/connect/github/authorize"' in client.get("/connect/github").text


class FakeGitHub:
    """httpx.get/post as GitHub answers them, for one user's token."""
    def __init__(self, me, installations, roles):
        self.me, self.installations, self.roles = me, installations, roles

    def post(self, url, **kw):
        class R:
            status_code = 200
            def json(self):
                return {"access_token": "ghu_x"}
        return R()

    def get(self, url, params=None, **kw):
        path = url.split("api.github.com", 1)[1]
        body, status = None, 200
        if path == "/user":
            body = self.me
        elif path == "/user/installations":
            body = {"installations": self.installations}
        elif path.startswith("/user/memberships/orgs/"):
            role = self.roles.get(path.rsplit("/", 1)[1])
            body, status = ({"role": role, "state": "active"}, 200) if role else ({}, 404)

        class R:
            status_code = status
            def json(self):
                return body
            def raise_for_status(self):
                if status >= 400:
                    raise RuntimeError(status)
        return R()


def test_control_means_your_own_account_or_an_org_you_administer(fresh_db, monkeypatch):
    db.set_setting("gh_app_client_id", "Iv1.abc")
    db.set_setting("gh_app_client_secret", "s")
    fake = FakeGitHub(
        me={"id": 1, "login": "ada"},
        installations=[
            {"id": 10, "account": {"type": "User", "id": 1, "login": "ada"}},
            {"id": 11, "account": {"type": "User", "id": 2, "login": "bob"}},
            {"id": 12, "account": {"type": "Organization", "login": "acme"}},
            {"id": 13, "account": {"type": "Organization", "login": "globex"}},
            {"id": 14, "account": {"type": "Organization", "login": "initech"}}],
        roles={"acme": "admin", "globex": "member"})
    monkeypatch.setattr(gh.httpx, "post", fake.post)
    monkeypatch.setattr(gh.httpx, "get", fake.get)
    assert set(gh.installations_user_controls("code")) == {10, 12}


def test_the_manifest_asks_github_for_authorization_on_install(fresh_db):
    m = gh.build_manifest("https://app.example.com")
    assert m["request_oauth_on_install"] is True
    assert "setup_url" not in m       # GitHub allows no setup_url alongside it
    assert m["default_permissions"]["members"] == "read"
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


def test_an_apply_that_blows_up_says_so_in_the_chat(fresh_db, monkeypatch):
    import asyncio
    said = []
    monkeypatch.setattr(main.medic, "apply_fix_sync",
                        lambda p, m: (_ for _ in ()).throw(RuntimeError("disk full")))
    monkeypatch.setattr(main.medic, "add_message", lambda p, role, text, kind: said.append(text))
    asyncio.run(main._apply_fix(1, 2))
    assert said == ["✖ Applying the fix failed: disk full"] and 2 not in main._applying


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

    for n in range(2, 14):                        # a year of renewals
        billing.handle_event(json.dumps({"type": "invoice.paid", "data": {"object": {
            "id": f"in_{n}", "customer": "cus_1"}}}).encode())
    days = (user(uid)["paid_until"] - db.now()) / 86400
    # 13 cycles and ONE grace period — the grace no longer piles up month on month
    assert 13 * billing.CYCLE_DAYS + billing.GRACE_DAYS - 1 < days \
        <= 13 * billing.CYCLE_DAYS + billing.GRACE_DAYS


# ---------- the test runner ----------

def test_customer_test_code_gets_a_copy_of_its_checkout_and_no_platform_volume(
        tmp_path, monkeypatch):
    """/data holds cicatrixa.db — the signing key, password hashes, the GitHub App
    key — and every tenant's checkout. The customer's suite must never see it."""
    import io
    import tarfile
    clone = tmp_path / "push" / "app-1"
    clone.mkdir(parents=True)
    (clone / "test_x.py").write_text("def test_x(): pass\n")
    seen = {}

    class Container:
        def put_archive(self, path, data):
            seen["names"] = tarfile.open(fileobj=io.BytesIO(data.read())).getnames()
        def start(self): seen["started"] = True
        def wait(self, timeout): return {"StatusCode": 0}
        def logs(self): return b"ok"
        def remove(self, force): pass

    class Containers:
        def create(self, image, **kw):
            seen["kw"] = kw
            return Container()

    class Dock:
        containers = Containers()
    monkeypatch.setattr(engine, "dock", lambda: Dock())
    assert engine.test_runner("img", str(clone))("pytest") == (0, "ok")
    assert "volumes" not in seen["kw"] and "mounts" not in seen["kw"]
    assert str(clone).lstrip("/") + "/test_x.py" in seen["names"] and seen["started"]


# ---------- deploys ----------

def test_a_candidate_takes_no_traffic_until_it_answers(monkeypatch):
    started = []

    class C:
        status = "running"
        def __init__(self, name): self.name = name
        def reload(self): pass
        def logs(self, tail): return b""

    def run(service, image, port, name, plan=None, routed=True):
        started.append((routed, port))
        return C(name)
    monkeypatch.setattr(engine, "_run_container", run)
    monkeypatch.setattr(engine, "_safe_rm", lambda c: None)
    monkeypatch.setattr(engine, "_exposed_ports", lambda image: [])
    monkeypatch.setattr(engine, "_probe", lambda host, ports, health, log, timeout: 3000)
    service = {"slug": "web", "project_id": 1, "user_id": 1, "name": "web"}
    port, name, _ = engine._start_and_probe(service, "img", {"port": 8000}, lambda m: None)
    assert started == [(False, 8000), (True, 3000)] and port == 3000


def test_an_unrouted_candidate_carries_no_routing_labels(monkeypatch):
    made = {}

    class Net:
        def connect(self, c, aliases): made["aliases"] = aliases
        def disconnect(self, c): pass

    class Client:
        class containers:
            @staticmethod
            def create(image, **kw):
                made.update(kw)
                class C:
                    def start(self): pass
                return C()
        class networks:
            @staticmethod
            def get(name): return Net()
    monkeypatch.setattr(engine, "dock", lambda: Client())
    monkeypatch.setattr(engine, "_sibling_env", lambda s: {})
    from app import dbprovision
    monkeypatch.setattr(dbprovision, "sibling_env", lambda pid: {})
    service = {"slug": "web", "project_id": 1, "user_id": 1, "name": "web"}
    engine._run_container(service, "img", 8000, "cx-web-1", {}, routed=False)
    assert made["labels"]["traefik.enable"] == "false"
    assert not any(k.startswith("traefik.http") for k in made["labels"])
    assert made["labels"]["cx.service"] == "web" and made["aliases"] == []


def test_https_app_routers_use_the_fixed_middleware_names(monkeypatch):
    monkeypatch.setattr(engine, "HTTPS_ENABLED", True)
    labels = engine._labels({"slug": "web", "project_id": 1, "user_id": 1, "name": "web"},
                            8000, {})
    assert labels["traefik.http.routers.cx-web.middlewares"] == "cx-app-web"
    assert labels["traefik.http.routers.cx-web-secure.middlewares"] == "cx-app-tls"


# ---------- live logs ----------

def test_the_event_stream_starts_at_once_and_never_goes_quiet(monkeypatch):
    import asyncio
    monkeypatch.setattr(bus, "HEARTBEAT", 0.01)

    async def first_two():
        stream = bus.subscribe("project:test")
        return [await stream.__anext__(), await stream.__anext__()]
    assert asyncio.run(first_two()) == [": connected\n\n", ": keep-alive\n\n"]


def test_a_stream_whose_client_left_ends_and_lets_go(monkeypatch):
    import asyncio
    monkeypatch.setattr(bus, "HEARTBEAT", 0.01)

    async def gone():
        return False

    async def drain():
        return [chunk async for chunk in bus.subscribe("project:gone", gone)]
    assert asyncio.run(drain()) == [": connected\n\n"]
    assert not bus._subscribers.get("project:gone")
