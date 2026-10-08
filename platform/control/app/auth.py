"""Password hashing (scrypt) and signed-cookie sessions — stdlib only."""
import base64
import hashlib
import hmac
import os
import secrets
import time

from . import db

SECRET_KEY = os.environ.get("SECRET_KEY") or ""
SESSION_TTL = 30 * 24 * 3600
COOKIE_NAME = "cx_session"
PENDING_COOKIE_NAME = "cx_pending"
PENDING_TTL = 15 * 60
CODE_TTL = 10 * 60
RESET_TTL = 60 * 60


def generate_code() -> str:
    """A 6-digit one-time verification code."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _secret() -> bytes:
    global SECRET_KEY
    if not SECRET_KEY:
        # persist an auto-generated secret so sessions survive restarts
        s = db.setting("secret_key")
        if not s:
            s = base64.urlsafe_b64encode(os.urandom(32)).decode()
            db.set_setting("secret_key", s)
        SECRET_KEY = s
    return SECRET_KEY.encode()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r = 8, p=1)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_b64, dk_b64 = stored.split("$", 1)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        dk = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1)
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def _sign(payload: str) -> str:
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _verify(token: str) -> str | None:
    try:
        payload, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    good = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return payload if hmac.compare_digest(sig, good) else None


def make_session(user_id: int) -> str:
    """Tied to the password it was issued under, like a reset link: setting a new
    password ends every session from before it, so a reset evicts whoever else was
    signed in. The prefix keeps every other token signed with this key — pending,
    reset, GitHub state — from ever reading as a session."""
    user = db.one("SELECT pw_hash FROM users WHERE id=?", (user_id,))
    tag = _password_tag(user["pw_hash"] if user else "")
    return sign_state(f"session:{user_id}:{tag}", ttl=SESSION_TTL)


def session_user(token: str | None):
    """The signed-in user, or None if the cookie is forged, expired, or from
    before the account's current password."""
    return _tagged_user(token, "session")


def session_user_id(token: str | None) -> int | None:
    user = session_user(token)
    return user["id"] if user else None


def make_pending(user_id: int) -> str:
    """The cookie that holds someone between signup and their code. Tied to the
    password like a session, so a reset by the address's real owner ends it."""
    user = db.one("SELECT pw_hash FROM users WHERE id=?", (user_id,))
    tag = _password_tag(user["pw_hash"] if user else "")
    return sign_state(f"pending:{user_id}:{tag}", ttl=PENDING_TTL)


def pending_user(token: str | None):
    return _tagged_user(token, "pending")


def pending_user_id(token: str | None) -> int | None:
    user = pending_user(token)
    return user["id"] if user else None


def _password_tag(pw_hash: str) -> str:
    """Binds a reset link to the password it replaces. Setting a new password
    changes the tag, so every link issued for the old one stops working: a link
    is single-use without a table to track it."""
    return hmac.new(_secret(), (pw_hash or "").encode(), hashlib.sha256).hexdigest()[:16]


def make_reset(user) -> str:
    return sign_state(f"reset:{user['id']}:{_password_tag(user['pw_hash'])}", ttl=RESET_TTL)


def reset_user(token: str | None):
    """The user a reset link is for, or None if it is forged, expired or used."""
    return _tagged_user(token, "reset")


def _tagged_user(token: str | None, kind: str):
    data = verify_state(token or "")
    if not data or not data.startswith(kind + ":"):
        return None
    try:
        _, uid, tag = data.split(":")
        user = db.one("SELECT * FROM users WHERE id=?", (int(uid),))
    except ValueError:
        return None
    if not user or not hmac.compare_digest(tag, _password_tag(user["pw_hash"])):
        return None
    return user


def make_gh_state(user_id: int) -> str:
    """The state sent through GitHub's install flow, naming who started it."""
    return sign_state(f"gh:{user_id}")


def gh_state_user_id(token: str | None) -> int | None:
    data = verify_state(token or "")
    if not data or not data.startswith("gh:"):
        return None
    try:
        return int(data.split(":", 1)[1])
    except ValueError:
        return None


def sign_state(data: str, ttl: int = 3600) -> str:
    """Short-lived signed state for OAuth-style redirects."""
    return _sign(f"{data}:{int(time.time()) + ttl}")


def verify_state(token: str) -> str | None:
    payload = _verify(token or "")
    if not payload:
        return None
    data, _, exp = payload.rpartition(":")
    try:
        if int(exp) < time.time():
            return None
    except ValueError:
        return None
    return data
