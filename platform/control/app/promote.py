"""Make an admin account, from the server itself:

    docker exec cx-control python -m app.promote you@example.com

Signing up proves nothing about who you are when email is off: anyone can type the
owner's address, and may have done so first. Running this needs a shell on the
machine, which is the proof the web cannot give — so it does not trust the
account's current password either. It creates the account if there is none, or
takes it over if there is: a new random password ends every session on it (the
squatter's included), and it prints a one-time link to choose your own.
With email on, an ADMIN_EMAILS address can instead become admin by entering its
emailed code at signup.
"""
import os
import secrets
import sys
from urllib.parse import urlencode

from . import auth, db


def promote(email: str) -> str:
    """Make `email` an admin with a password nobody knows yet. Returns a link to
    set one."""
    email = email.strip().lower()
    pw_hash = auth.hash_password(secrets.token_urlsafe(32))
    row = db.one("SELECT id FROM users WHERE email=?", (email,))
    if row:
        db.q("UPDATE users SET pw_hash=?, is_admin=1, email_verified=1, verify_code=NULL, "
             "verify_expires=NULL WHERE id=?", (pw_hash, row["id"]))
        uid = row["id"]
    else:
        uid = db.q("INSERT INTO users(email,pw_hash,is_admin,email_verified,created_at) "
                   "VALUES(?,?,1,1,?)", (email, pw_hash, db.now())).lastrowid
    user = db.one("SELECT * FROM users WHERE id=?", (uid,))
    base = os.environ.get("BASE_URL") or f"http://{os.environ.get('BASE_DOMAIN', 'localhost')}"
    return f"{base}/reset-password?" + urlencode({"token": auth.make_reset(user)})


def main(argv: list[str]) -> int:
    if len(argv) != 2 or "@" not in argv[1]:
        print("usage: python -m app.promote you@example.com", file=sys.stderr)
        return 2
    # Read the live database as it is: no schema work from a side door, and no
    # empty database conjured up at a wrong path.
    if not os.path.exists(db.DB_PATH):
        print(f"no database at {db.DB_PATH} — run this inside cx-control", file=sys.stderr)
        return 1
    link = promote(argv[1])
    email = argv[1].strip().lower()
    print(f"{email} is an admin. Open this link within an hour to choose its password")
    print("(it works once; any session already open on this account has ended):")
    print(f"  {link}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
