"""Invite-gated signup: an admin sends invites directly, or approves/rejects
access requests from people who show up without one."""
import secrets

from . import db, mailer


def bootstrap_open() -> bool:
    """The very first account on a fresh instance always gets straight in."""
    return not db.one("SELECT 1 FROM users LIMIT 1")


def admin_listed(email: str, admin_emails: set[str]) -> bool:
    return email.strip().lower() in admin_emails


def request_access(email: str) -> str:
    """A stranger asks for an account. Returns a user-facing status message."""
    email = email.strip().lower()
    existing = db.one("SELECT status FROM invites WHERE email=? ORDER BY id DESC",
                      (email,))
    if existing and existing["status"] in ("pending", "approved"):
        return "You're already on the list — an admin will review it shortly."
    token = secrets.token_urlsafe(24)
    db.q("INSERT INTO invites(email,token,origin,status,created_at) "
         "VALUES(?,?,'requested','pending',?)", (email, token, db.now()))
    return "Request received — an admin will review it shortly."


def send_invite(email: str, base_url: str, decided_by: int | None = None) -> bool:
    """Admin-initiated: create an already-approved invite and email it."""
    email = email.strip().lower()
    token = secrets.token_urlsafe(24)
    db.q("INSERT INTO invites(email,token,origin,status,decided_by,created_at,decided_at) "
         "VALUES(?,?,'admin_sent','approved',?,?,?)",
         (email, token, decided_by, db.now(), db.now()))
    return mailer.send_invite(email, f"{base_url}/signup?invite={token}")


def approve(invite_id: int, base_url: str, decided_by: int) -> bool:
    row = db.one("SELECT * FROM invites WHERE id=? AND status='pending'", (invite_id,))
    if not row:
        return False
    db.q("UPDATE invites SET status='approved', decided_by=?, decided_at=? WHERE id=?",
         (decided_by, db.now(), invite_id))
    return mailer.send_invite(row["email"], f"{base_url}/signup?invite={row['token']}")


def revoke(invite_id: int, decided_by: int):
    db.q("UPDATE invites SET status='revoked', decided_by=?, decided_at=? "
         "WHERE id=? AND status IN ('pending','approved')",
         (decided_by, db.now(), invite_id))


def resolve(token: str, email: str) -> tuple[bool, str]:
    """Validate a token at signup time. Returns (ok, error_message)."""
    row = db.one("SELECT * FROM invites WHERE token=?", (token,))
    if not row:
        return False, "That invite link isn't valid."
    if row["status"] == "used":
        return False, "That invite has already been used."
    if row["status"] != "approved":
        return False, "That invite is no longer valid."
    if row["email"] != email.strip().lower():
        return False, "That invite was issued to a different email address."
    return True, ""


def mark_used(token: str):
    db.q("UPDATE invites SET status='used', decided_at=? WHERE token=?",
         (db.now(), token))


def pending_requests() -> list:
    return db.all_("SELECT * FROM invites WHERE origin='requested' AND status='pending' "
                   "ORDER BY id DESC")


def sent_invites() -> list:
    return db.all_("SELECT * FROM invites WHERE status='approved' ORDER BY id DESC")
