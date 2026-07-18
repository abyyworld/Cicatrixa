"""Password hashing (scrypt) and signed-cookie sessions — stdlib only."""
import base64
import hashlib
import hmac
import os
import time

from . import db

SECRET_KEY = os.environ.get("SECRET_KEY") or ""
SESSION_TTL = 30 * 24 * 3600
COOKIE_NAME = "cx_session"


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
    return _sign(f"{user_id}:{int(time.time()) + SESSION_TTL}")


def session_user_id(token: str | None) -> int | None:
    if not token:
        return None
    payload = _verify(token)
    if not payload:
        return None
    try:
        uid, exp = payload.split(":")
        if int(exp) < time.time():
            return None
        return int(uid)
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
