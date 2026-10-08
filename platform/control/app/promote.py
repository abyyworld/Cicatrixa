"""Make an existing account an admin, from the server itself:

    docker exec cx-control python -m app.promote you@example.com

Signing up proves nothing about who you are when email is off: anyone can type the
owner's address. Running this needs a shell on the machine, which is the proof the
web cannot give. With email on, an address in ADMIN_EMAILS becomes admin by itself
once its emailed code is entered; this is for everything else.
"""
import os
import sys

from . import db


def promote(email: str) -> bool:
    email = email.strip().lower()
    row = db.one("SELECT id FROM users WHERE email=?", (email,))
    if not row:
        return False
    db.q("UPDATE users SET is_admin=1 WHERE id=?", (row["id"],))
    return True


def main(argv: list[str]) -> int:
    if len(argv) != 2 or "@" not in argv[1]:
        print("usage: python -m app.promote you@example.com", file=sys.stderr)
        return 2
    # Read the live database as it is: no schema work from a side door, and no
    # empty database conjured up at a wrong path.
    if not os.path.exists(db.DB_PATH):
        print(f"no database at {db.DB_PATH} — run this inside cx-control", file=sys.stderr)
        return 1
    if not promote(argv[1]):
        print(f"no account for {argv[1]} — sign up first, then run this again",
              file=sys.stderr)
        return 1
    print(f"{argv[1].strip().lower()} is now an admin.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
