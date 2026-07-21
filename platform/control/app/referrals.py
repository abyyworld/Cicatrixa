"""User-to-user invites and the referral programme.

Every member gets a personal invite link (/signup?ref=CODE). Anyone who signs
up through it skips the request-access queue — members vouching for people is
still invite-only, just distributed. When a referred user converts to paid,
the referrer earns a +20% quota bonus (RAM, disk, services), stacking up to
+100%.
"""
import secrets

from . import db

BONUS_PCT_PER_CONVERSION = 20
BONUS_PCT_CAP = 100
TRIAL_DAYS = 14


def code_for(user) -> str:
    """Return the user's referral code, minting one on first use."""
    if user["referral_code"]:
        return user["referral_code"]
    while True:
        code = secrets.token_urlsafe(6)
        if not db.one("SELECT 1 FROM users WHERE referral_code=?", (code,)):
            break
    db.q("UPDATE users SET referral_code=? WHERE id=?", (code, user["id"]))
    return code


def referrer_for(code: str):
    """Look up the user who owns a referral code (None if invalid)."""
    if not code:
        return None
    return db.one("SELECT * FROM users WHERE referral_code=?", (code,))


def stats_for(user_id: int) -> dict:
    joined = db.one("SELECT COUNT(*) c FROM users WHERE referred_by=?",
                    (user_id,))["c"]
    converted = db.one("SELECT COUNT(*) c FROM users WHERE referred_by=? "
                       "AND referral_converted=1", (user_id,))["c"]
    return {"joined": joined, "converted": converted,
            "bonus_pct": bonus_pct(user_id)}


def bonus_pct(user_id: int) -> int:
    converted = db.one("SELECT COUNT(*) c FROM users WHERE referred_by=? "
                       "AND referral_converted=1", (user_id,))["c"]
    return min(converted * BONUS_PCT_PER_CONVERSION, BONUS_PCT_CAP)


def record_conversion(user_id: int):
    """Called when a referred user becomes a paying customer. Marks the
    conversion exactly once; the referrer's bonus is derived from the count."""
    user = db.one("SELECT * FROM users WHERE id=?", (user_id,))
    if not user or not user["referred_by"] or user["referral_converted"]:
        return
    db.q("UPDATE users SET referral_converted=1 WHERE id=?", (user_id,))


# ---------- trial / paid state ----------

def trial_ends_at(user) -> float:
    return user["created_at"] + TRIAL_DAYS * 86400


def is_paid(user) -> bool:
    return bool(user["paid_until"] and user["paid_until"] > db.now())


def trial_days_left(user) -> int:
    left = (trial_ends_at(user) - db.now()) / 86400
    return max(0, int(left) + (1 if left % 1 > 0 else 0))


def can_deploy(user) -> tuple[bool, str]:
    """Deploy gate: admins always, paid users always, trial users until day 14.
    Running services are never touched — this only gates new work."""
    if user["is_admin"] or is_paid(user):
        return True, ""
    if db.now() <= trial_ends_at(user):
        return True, ""
    return False, ("Your 14-day trial has ended. Subscribe ($2.99/project/mo) to "
                   "keep deploying — your running services stay up either way.")
