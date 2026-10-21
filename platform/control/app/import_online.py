"""Bring the online workspace onto this box, once it runs Docker:

    docker exec cx-control python -m app.import_online --dry-run
    docker exec cx-control python -m app.import_online
      ... app.<domain> moves here, the import once more, verify-public.sh ...
    docker exec cx-control python -m app.import_online --start
    docker restart cx-control
    docker exec cx-control python -m app.import_online --notify

Until the platform had a Docker host, app.cicatrixa.com ran on Vercel (accounts/,
workspace/): people signed up, connected GitHub through the same GitHub App, and
planned projects nothing could build yet. This copies all of it out of that Turso
database: every user (Better Auth's password hashes work here unchanged — see
auth.verify_password), the GitHub App's credentials, every GitHub connection and every
planned project. docs/RUNBOOK.md, "Leaving Vercel", has the order to do it in.

Nothing imported deploys until `--start` says so: it records that app.<domain> is here
and verified, and from then on each start of cx-control deploys the imported projects
that wait (main.startup). Before it, a restart — `docker compose up -d` after a fix to
.env, at any earlier step — deploys nothing, so no customer app runs before the move
and verify-public.sh may still restart Traefik.

The import emails nobody. `--notify` is its own step, for once the box serves
app.<domain> and the imported projects have started deploying: it tells each owner
whose projects started, records every email that went out, and a later run sends only
what is still owed.

An account already on the box is never merged with an online one by default: an
address proves nothing until someone has shown they own it, and whoever signed up
here first would get the online person's projects and GitHub installation. Such
people are listed and left out; `--link-existing a@x,b@y` merges the addresses it
names, after the operator has looked at each, and even then only when both sides
confirmed the address. It names them one by one: an account squatted here sits in
the same list as the operator's own.

Safe to run again: what it brought over carries the workspace's own id (external_id).
A second run adds what is new, carries over what changed online since (a password reset,
while the account here still uses its online password; an email confirmed since),
removes projects deleted online that never deployed here and GitHub connections it
brought over that were disconnected online since, and never brings back a project
deleted here. Everything is written in one transaction; --dry-run rolls it back after
printing what it would have done.

It reads TURSO_DATABASE_URL, TURSO_AUTH_TOKEN and BETTER_AUTH_SECRET (the values Vercel
has) from cx-control's environment, i.e. platform/.env.
"""
import argparse
import base64
import contextlib
import hashlib
import html
import os
import re
import secrets
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import auth, db, engine, mailer, metrics, referrals

NEEDS_SCHEMA = 4            # db.MIGRATIONS 3 and 4 add the external_id columns
ENC_PREFIX = "enc1:"
# settings key, written in the import's own transaction: from its commit on, an import
# has happened here, and main.py opens sign-ups (they wait for it while
# TURSO_DATABASE_URL is set).
IMPORTED_KEY = "online_import_at"
# settings key `--start` writes (RUNBOOK "Leaving Vercel" step 7, once app.<domain> is
# here and verified): only from then on does main.startup deploy imported services.
START_KEY = "online_import_start_at"
# The online cx_setting row accounts/auth.js writes when a password is set through an
# emailed reset link: pw_proof:<userId>. The one proof that whoever holds the
# password also holds the inbox — confirming an address only shows that someone with
# the inbox clicked, maybe on a link a squatter's sign-up sent.
PROOF_PREFIX = "pw_proof:"
# Resend allows 2 requests a second; a little under it.
SEND_INTERVAL = 0.6
# Resend's daily cap (100 on the free plan) answers every send after it the same way.
FAILURES_IN_A_ROW = 3

# The workspace's settings, under the platform's own key names. Only these are
# copied: nothing else in that table is the workspace's to set here (a secret_key or
# schema_version would take over this box's sessions or its schema).
APP_SETTINGS = ("gh_app_id", "gh_app_slug", "gh_app_name", "gh_app_client_id",
                "gh_app_pem", "gh_app_webhook_secret", "gh_app_client_secret")

# main.RESERVED_SLUGS, repeated so this command does not load the whole web app.
# tests/test_import_online.py keeps the two identical.
RESERVED_SLUGS = {"app", "www", "api", "admin", "demo", "healer", "traefik", "mail",
                  "status", "docs"}

# Rows from another system end up in Traefik Host rules, container names and git
# command lines here, so they are checked, not trusted. The patterns admit everything
# the workspace's own slug and naming rules (ports of main.py's) produce.
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_BETTER_AUTH_HASH = re.compile(r"[0-9a-f]+:[0-9a-f]+")

READS = {   # name -> (table it needs, query)
    "users": ("user", 'select id, email, emailVerified from "user" order by createdAt, id'),
    "credentials": ("account", "select userId, password from account "
                               "where providerId = 'credential'"),
    "settings": ("cx_setting", "select key, value from cx_setting"),
    "github": ("cx_github", "select userId, installationId, accountLogin from cx_github"),
    "projects": ("cx_project", "select id, userId, name, slug, createdAt from cx_project "
                               "order by createdAt, id"),
    "services": ("cx_service", "select id, projectId, name, slug, repoFull, branch "
                               "from cx_service order by projectId, isPrimary desc, "
                               "createdAt, id"),
}

# An imported service that has never been tried here: what main.startup deploys.
# ('deploying' with no deployment: admitted by a start that ended before it began.)
WAITING = ("s.status IN ('new','deploying') AND NOT EXISTS "
           "(SELECT 1 FROM deployments d WHERE d.service_id=s.id)")
# One that has started here: admitted by main.startup, or deployed at least once.
STARTED = ("(s.status='deploying' OR EXISTS "
           "(SELECT 1 FROM deployments d WHERE d.service_id=s.id))")


class Refused(Exception):
    """The command cannot run as things stand. The message says why, for the operator."""


# ---------- reading Turso ----------

def turso_http_url(url: str) -> str:
    """libsql://HOST, as Turso hands it out, is served over HTTP at https://HOST."""
    parts = urlsplit(url.strip())
    scheme = {"libsql": "https", "wss": "https", "https": "https",
              "ws": "http", "http": "http"}.get(parts.scheme)
    if not scheme or not parts.hostname:
        # Not echoed: such a URL can carry ?authToken=.
        raise Refused("TURSO_DATABASE_URL is not a libsql:// URL "
                      "(it looks like libsql://cicatrixa-prod-<org>.<region>.turso.io)")
    return f"{scheme}://{parts.netloc}"


def _post_pipeline(base: str, token: str, body: dict) -> dict:
    """The one network call: Turso's HTTP API (Hrana over HTTP, /v2/pipeline)."""
    try:
        r = httpx.post(f"{base}/v2/pipeline", json=body, timeout=60,
                       headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise Refused(f"could not reach Turso at {base}: {type(exc).__name__}: {exc}") from exc
    if r.status_code in (401, 403):
        raise Refused(f"Turso refused the token ({r.status_code}): check TURSO_AUTH_TOKEN")
    if r.status_code >= 300:
        raise Refused(f"Turso answered {r.status_code}: {r.text[:300]} (an archived free "
                      "database answers like this: `turso group unarchive default`)")
    return r.json()


def _value(v: dict):
    kind = v.get("type")
    if kind == "null":
        return None
    if kind == "integer":
        return int(v["value"])      # sent as a string: 64-bit integers outgrow JSON numbers
    if kind == "float":
        return float(v["value"])
    if kind == "text":
        return v["value"]
    if kind == "blob":
        return base64.b64decode(v.get("base64") or "")
    raise Refused(f"Turso sent a value of unknown type {kind!r}")


def query(base: str, token: str, sqls: list[str]) -> list[list[dict]]:
    """Runs each statement, in one request, and returns each one's rows as dicts."""
    body = {"requests": [*({"type": "execute", "stmt": {"sql": s}} for s in sqls),
                         {"type": "close"}]}
    results = _post_pipeline(base, token, body).get("results") or []
    if len(results) < len(sqls):
        raise Refused("Turso's answer was cut short")
    out = []
    for sql, res in zip(sqls, results):
        if res.get("type") != "ok":
            why = (res.get("error") or {}).get("message", "unknown error")
            raise Refused(f"Turso could not run `{sql}`: {why}")
        result = res["response"]["result"]
        cols = [c.get("name") for c in result["cols"]]
        out.append([dict(zip(cols, map(_value, row))) for row in result["rows"]])
    return out


def fetch(base: str, token: str) -> dict[str, list[dict]]:
    """Everything the import needs, read before anything here is written. The
    workspace's tables appear on its first request; until then there are none."""
    [tables] = query(base, token, ["select name from sqlite_master where type = 'table'"])
    present = {t["name"] for t in tables}
    if not {"user", "account"} <= present:
        raise Refused("that database has no Better Auth tables: TURSO_DATABASE_URL must be "
                      "the accounts database Vercel uses (cicatrixa-prod)")
    wanted = [name for name, (table, _) in READS.items() if table in present]
    data = {name: [] for name in READS}
    data.update(zip(wanted, query(base, token, [READS[n][1] for n in wanted])))
    data["tables"] = tables
    return data


def decrypt_setting(secret: str, stored: str) -> str:
    """workspace/secrets.js decryptSetting: "enc1:" + base64url(iv[12] ‖ ciphertext ‖
    tag[16]), AES-256-GCM, no AAD, key = SHA-256 of "cicatrixa-settings\\0" + the
    BETTER_AUTH_SECRET. A value without the prefix was stored plain."""
    if not stored.startswith(ENC_PREFIX):
        return stored
    raw = stored[len(ENC_PREFIX):]
    blob = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    if len(blob) < 12 + 16:
        raise ValueError("the value is truncated")
    key = hashlib.sha256(f"cicatrixa-settings\0{secret}".encode()).digest()
    try:
        return AESGCM(key).decrypt(blob[:12], blob[12:], None).decode()
    except InvalidTag:
        raise ValueError("it does not decrypt with this BETTER_AUTH_SECRET") from None


# ---------- writing here ----------

@dataclass
class Report:
    dry_run: bool = False
    link_existing: frozenset = frozenset()      # the addresses --link-existing names
    started: bool = False                       # --start has run here
    admin_emails_set: bool = True
    mail_on: bool = True
    app: str = "none online: nothing copied"
    users_new: int = 0
    users_before: int = 0
    users_linked: list[str] = field(default_factory=list)
    users_existing: list[str] = field(default_factory=list)   # not merged: see the docstring
    link_unused: list[str] = field(default_factory=list)      # named, but nobody to link
    users_refreshed: list[str] = field(default_factory=list)  # password carried over again
    users_confirmed: list[str] = field(default_factory=list)  # confirmed online since
    no_password: list[str] = field(default_factory=list)
    admins: list[str] = field(default_factory=list)
    admins_unproven: list[str] = field(default_factory=list)
    users_skipped: list[str] = field(default_factory=list)
    connections_new: int = 0
    connections_before: int = 0
    connections_skipped: list[str] = field(default_factory=list)
    connections_removed: list[str] = field(default_factory=list)
    projects_new: int = 0
    services_new: int = 0
    projects_before: int = 0
    projects_skipped: list[str] = field(default_factory=list)
    projects_deleted_here: list[str] = field(default_factory=list)
    projects_removed: list[str] = field(default_factory=list)
    projects_gone_kept: list[str] = field(default_factory=list)
    held_back: list[str] = field(default_factory=list)
    capacity: list[str] = field(default_factory=list)

    def summary(self) -> str:
        out = [("DRY RUN, nothing was written. " if self.dry_run else "")
               + f"Online workspace -> {db.DB_PATH}"]
        if not self.mail_on:
            out += ["  ⚠ RESEND_API_KEY is empty here, and these people had email online. Put "
                    "Vercel's",
                    "    RESEND_API_KEY and MAIL_FROM in platform/.env and `docker compose up -d` "
                    "first: without",
                    "    them nobody can reset a forgotten password themselves, sign-ups here are "
                    "confirmed",
                    "    without proof, and --notify cannot tell anyone their projects deploy."]
        out += [f"  GitHub App: {self.app}",
                f"  users: {self.users_new} new, {len(self.users_linked)} linked to an account "
                f"already here (it keeps its own password), {self.users_before} imported before"]
        out += [f"    = {e}" for e in self.users_linked]
        out += [f"    ✗ {s}" for s in self.users_skipped]
        if self.users_existing:
            out.append(f"  {len(self.users_existing)} not brought over: an account with the "
                       "same address was made here, and nothing shows it is the same person")
            out += [f"    ✗ {s}" for s in self.users_existing]
            out.append("    each is left out with their GitHub connection and projects. If "
                       "an account above is theirs, run again with --link-existing and its "
                       "address (a@x,b@y for more): only the addresses named are merged")
        out += [f"    ! --link-existing names {e}, but no online account with that address "
                "has one here: nothing linked" for e in self.link_unused]
        out += [f"    ↻ {e}: password changed online since; carried over" for e in
                self.users_refreshed]
        out += [f"    ↻ {e}: email confirmed online since" for e in self.users_confirmed]
        if self.no_password:
            out.append(f"  {len(self.no_password)} with no password online: they choose one "
                       "with \"Forgot your password?\" here")
            out += [f"    - {e}" for e in self.no_password]
        admins = ", ".join(self.admins) or "none"
        if not self.admin_emails_set:
            admins += (" (ADMIN_EMAILS is empty here: make yours with "
                       "`docker exec cx-control python -m app.promote you@example.com`)")
        out.append(f"  new admins: {admins}")
        for e in self.admins_unproven:
            out.append(f"    ! {e} is in ADMIN_EMAILS but its password was never set through an "
                       "emailed reset link online: not admin here (`python -m app.promote`)")
        out.append(f"  GitHub connections: {self.connections_new} new, "
                   f"{self.connections_before} imported before")
        out += [f"    ✗ {s}" for s in self.connections_skipped]
        out += [f"    − {s}" for s in self.connections_removed]
        out.append(f"  projects: {self.projects_new} new ({self.services_new} services), "
                   f"{self.projects_before} imported before")
        out += [f"    ✗ {s}" for s in self.projects_skipped]
        out += [f"    ✗ {s}: deleted here since it was imported; not brought back"
                for s in self.projects_deleted_here]
        out += [f"    − {s}: deleted online and never deployed here; removed"
                for s in self.projects_removed]
        out += [f"    ! {s}" for s in self.projects_gone_kept]
        if self.held_back:
            out.append("  held back (they wait; once --start has run, the first restart of "
                       "cx-control after that changes deploys them):")
            out += [f"    ✗ {s}" for s in self.held_back]
        out += [f"  {line}" for line in self.capacity]
        if self.started and not self.dry_run:
            out.append("  --start has run here: the next `docker restart cx-control` deploys "
                       "what waits; --notify after")
        elif not self.dry_run:
            out.append("  nothing deploys yet. next: docs/RUNBOOK.md, Leaving Vercel — move app "
                       "and * here, run this again, verify, then --start and `docker restart "
                       "cx-control` deploy what waits; --notify after")
        return "\n".join(out)


@contextlib.contextmanager
def _write_transaction(c: sqlite3.Connection, commit: bool):
    """One transaction for the whole import, rolled back on any error or a dry run.

    BEGIN IMMEDIATE, because cx-control keeps writing while this runs (health checks,
    metrics): a deferred transaction that read first could not upgrade to a write
    after one of theirs, and SQLite fails that at once instead of waiting. Taking the
    write lock first makes their writes wait out ours instead. Autocommit off and an
    explicit BEGIN, as in db._transaction; and only c.execute inside — db.q commits."""
    previous = c.isolation_level
    c.isolation_level = None
    try:
        c.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("COMMIT" if commit else "ROLLBACK")
    finally:
        c.isolation_level = previous


def _truthy(v) -> bool:
    return v in (1, True, "1", "true")


def _epoch(value, default: float) -> float:
    """Better Auth and the workspace store times as ISO text."""
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)    # epoch ms or s
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return default


def _day(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(epoch or 0))


def _branch_ok(branch: str) -> bool:
    return (0 < len(branch) <= 255 and not branch.startswith("-")
            and not any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in branch))


def _copy_settings(c, data, report: Report, secret: str):
    online = {r["key"]: r["value"] for r in data["settings"] if r["key"] in APP_SETTINGS}
    app_id = str(online.get("gh_app_id") or "")
    if not app_id:
        return
    row = c.execute("SELECT value FROM settings WHERE key='gh_app_id'").fetchone()
    here = row["value"] if row else ""
    # An installation belongs to the App it was made for: this box's own App could not
    # get a token for a single one of them, so every connection would be dead.
    if here and here != app_id:
        raise Refused(f"this box already has a different GitHub App (id {here}; the online "
                      f"one is {app_id}), and the online connections only work under the "
                      "online one. "
                      "Delete the box's gh_app_* settings first if the online App is the one "
                      "to keep.")
    values = {}
    for key, stored in online.items():
        try:
            values[key] = decrypt_setting(secret, str(stored or ""))
        except (ValueError, TypeError) as exc:
            raise Refused(f"cannot read {key}: {exc}. BETTER_AUTH_SECRET must be the very "
                          "value Vercel has.") from None
    slug = values.get("gh_app_slug") or "?"
    if here:
        # The same App, brought over before: fill what is missing, overwrite nothing.
        missing = [k for k in APP_SETTINGS if values.get(k) and not c.execute(
            "SELECT 1 FROM settings WHERE key=? AND value<>''", (k,)).fetchone()]
        for key in missing:
            c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) "
                      "DO UPDATE SET value=excluded.value", (key, values[key]))
        report.app = f"id {app_id} ({slug}) already here" + (
            f", filled in {', '.join(missing)}" if missing else "")
        return
    for key, value in values.items():
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) "
                  "DO UPDATE SET value=excluded.value", (key, value))
    report.app = f"id {app_id} ({slug}) copied"


def _account_here(c, row) -> str:
    """What the operator needs to judge whether an account here is the online person's."""
    projects = c.execute("SELECT COUNT(*) n FROM projects WHERE user_id=?",
                         (row["id"],)).fetchone()["n"]
    github = c.execute("SELECT gh_login FROM github_connections WHERE user_id=? "
                       "ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
    return (f"made here {_day(row['created_at'])}, "
            f"email {'confirmed' if row['email_verified'] else 'NOT confirmed'}, "
            f"{projects} project{'' if projects == 1 else 's'}, "
            + (f"GitHub connected ({github['gh_login'] or '?'})" if github else "no GitHub"))


def _link_refusal(c, row, online_verified: bool, named: bool) -> str | None:
    """Why the online account must not be merged into the account `row` here, if so.

    Merging hands that account the online person's projects and GitHub installation,
    and an address is all the two share. The box takes sign-ups by any host name once
    `*` points at it, and with email off it confirms them without proof — so by
    default nothing is merged, an address is merged only when --link-existing names it
    (`named`), and even then not without both sides having confirmed it."""
    if row["external_id"]:
        return "the account here is already linked to another online account"
    facts = _account_here(c, row)
    if not named:
        return f"already has an account here ({facts})"
    if not row["email_verified"]:
        return f"the account here never confirmed its email ({facts}); not linked"
    if not online_verified:
        return f"the online account never confirmed its email ({facts}); not linked"
    return None


def _refresh_user(c, row, email: str, verified: bool, online_hash: str | None,
                  admin: bool, report: Report):
    """Carry over what changed online since the last run, for an account that still
    follows its online one: its hash is still Better Auth's (auth.hash_password writes
    another format, so a password set here is never overwritten)."""
    follows = bool(_BETTER_AUTH_HASH.fullmatch(row["pw_hash"] or ""))
    if follows and online_hash and online_hash != row["pw_hash"]:
        c.execute("UPDATE users SET pw_hash=? WHERE id=?", (online_hash, row["id"]))
        report.users_refreshed.append(email)
    if verified and not row["email_verified"]:
        c.execute("UPDATE users SET email_verified=1 WHERE id=?", (row["id"],))
        report.users_confirmed.append(email)
    if follows and admin and not row["is_admin"]:
        c.execute("UPDATE users SET is_admin=1 WHERE id=?", (row["id"],))
        report.admins.append(email)


def _import_users(c, data, report: Report, admin_emails: set[str], now: float,
                  link_existing: frozenset):
    """Returns {online user id: user id here} for everyone who arrived or was here.
    `link_existing`: the addresses whose accounts here may be merged (_link_refusal)."""
    passwords = {str(r["userId"]): r["password"] for r in data["credentials"]}
    proofs = {str(r["key"])[len(PROOF_PREFIX):] for r in data["settings"]
              if str(r["key"]).startswith(PROOF_PREFIX)}
    mapped: dict[str, int] = {}
    met: set[str] = set()                       # addresses with an account here to link
    for u in data["users"]:
        ext = str(u["id"] or "")
        email = (u["email"] or "").strip().lower()
        if not ext or "@" not in email:
            report.users_skipped.append(f"online user {ext or '?'}: no usable email")
            continue
        verified = _truthy(u["emailVerified"])
        pw_hash = passwords.get(ext)
        if not (isinstance(pw_hash, str) and _BETTER_AUTH_HASH.fullmatch(pw_hash)):
            pw_hash = None
        # ADMIN_EMAILS names the address; a confirmed email shows only that someone with
        # the inbox clicked. The password proof shows the password's holder has it too.
        admin = verified and email in admin_emails and ext in proofs
        row = c.execute("SELECT * FROM users WHERE external_id=?", (ext,)).fetchone()
        if row:
            mapped[ext] = row["id"]
            report.users_before += 1
            _refresh_user(c, row, email, verified, pw_hash, admin, report)
            continue
        row = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if row:
            met.add(email)
            why = _link_refusal(c, row, verified, email in link_existing)
            if why:
                listed = report.users_skipped if row["external_id"] else report.users_existing
                listed.append(f"{email}: {why}")
                continue
            # Their account here stays as it is, password included; it only learns
            # which online account it is, so the projects and connection find it.
            c.execute("UPDATE users SET external_id=? WHERE id=?", (ext, row["id"]))
            mapped[ext] = row["id"]
            report.users_linked.append(email)
            continue
        if pw_hash is None:
            # Only email sign-in exists online, so this should not happen; if it does,
            # a password nobody knows, and "Forgot your password?" sets a real one.
            pw_hash = auth.hash_password(secrets.token_urlsafe(32))
            report.no_password.append(email)
        # created_at is now, not their online sign-up: the 14-day trial is for
        # deploying, which starts today.
        mapped[ext] = c.execute(
            "INSERT INTO users(email,pw_hash,is_admin,email_verified,created_at,external_id)"
            " VALUES(?,?,?,?,?,?)",
            (email, pw_hash, 1 if admin else 0, 1 if verified else 0, now, ext)).lastrowid
        report.users_new += 1
        if admin:
            report.admins.append(email)
        elif verified and email in admin_emails:
            report.admins_unproven.append(email)
    report.link_unused = sorted(link_existing - met)
    return mapped


def _remove_gone_connections(c, data, report: Report, emails: dict[str, str]):
    """GitHub connections this command brought over whose online row is gone since:
    their owner pressed Disconnect online (or connected another installation, which
    _import_connections then brings over). Kept here, it would clone and deploy their
    projects through an installation they disconnected; removed, _hold_back lists them.

    Only connections this command made (external_id), never one made here; and only
    when the online database has the table at all — without it, every connection
    would look disconnected."""
    if "cx_github" not in {t["name"] for t in data.get("tables", [])}:
        return
    online = {(str(g["userId"]), int(g["installationId"])) for g in data["github"]}
    for row in c.execute("SELECT * FROM github_connections WHERE external_id IS NOT NULL "
                         "ORDER BY id").fetchall():
        if (row["external_id"], row["installation_id"]) in online:
            continue
        c.execute("DELETE FROM github_connections WHERE id=?", (row["id"],))
        who = emails.get(row["external_id"], row["external_id"])
        report.connections_removed.append(f"{who}: installation {row['installation_id']} "
                                          "was disconnected online since; removed here")


def _import_connections(c, data, report: Report, mapped: dict[str, int],
                        emails: dict[str, str], now: float):
    for g in data["github"]:
        ext = str(g["userId"])
        uid = mapped.get(ext)
        if uid is None:
            continue                                    # their user was skipped, and said so
        who = emails.get(ext, ext)
        iid = int(g["installationId"])
        if c.execute("SELECT 1 FROM github_connections WHERE kind='app' AND installation_id=? "
                     "AND user_id<>?", (iid, uid)).fetchone():
            # Same rule as main.connect_setup: one installation, one account, ever.
            report.connections_skipped.append(f"{who}: installation {iid} is connected to "
                                              "another account here")
            continue
        if c.execute("SELECT 1 FROM github_connections WHERE kind='app' AND installation_id=? "
                     "AND user_id=?", (iid, uid)).fetchone():
            report.connections_before += 1
            continue
        if c.execute("SELECT 1 FROM github_connections WHERE kind='app' AND user_id=?",
                     (uid,)).fetchone():
            report.connections_skipped.append(f"{who}: already connected GitHub here; "
                                              "kept that connection")
            continue
        c.execute("INSERT INTO github_connections(user_id,kind,installation_id,gh_login,"
                  "created_at,external_id) VALUES(?,?,?,?,?,?)",
                  (uid, "app", iid, g["accountLogin"] or "", now, ext))
        report.connections_new += 1


def _slug_taken(c, slug: str) -> bool:
    return bool(slug in RESERVED_SLUGS
                or c.execute("SELECT 1 FROM projects WHERE slug=?", (slug,)).fetchone()
                or c.execute("SELECT 1 FROM services WHERE slug=?", (slug,)).fetchone())


def _project_problem(c, project: dict, services: list[dict]) -> str | None:
    if not services:
        return "it has no services"
    slugs = [project["slug"]] + [s["slug"] for s in services]
    for slug in slugs:
        if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
            return f"malformed slug {slug!r}"
    for s in services:
        if not isinstance(s["repoFull"], str) or not _REPO.fullmatch(s["repoFull"]):
            return f"malformed repository {s['repoFull']!r}"
        if not _branch_ok(str(s["branch"] or "")):
            return f"malformed branch {s['branch']!r}"
    # The primary service shares its project's slug; no two services share one.
    if len({s["slug"] for s in services}) != len(services):
        return "two of its services share an address"
    for slug in dict.fromkeys(slugs):
        if _slug_taken(c, slug):
            return f"{slug}.{engine.BASE_DOMAIN} is taken here"
    return None


def _label(p) -> str:
    return f"{p['name']} ({p['slug']})"


def _remove_gone_projects(c, data, report: Report):
    """Projects deleted online since they were imported. One that never deployed here
    goes too — nobody has seen it here, and startup would otherwise build what its
    owner threw away. One that has deployed is the owner's to delete here.

    Only when the online database has the projects table at all: without it, every
    project would look deleted."""
    if "cx_project" not in {t["name"] for t in data.get("tables", [])}:
        return
    online = {str(p["id"]) for p in data["projects"]}
    for p in c.execute("SELECT * FROM projects WHERE external_id IS NOT NULL "
                       "ORDER BY id").fetchall():
        if p["external_id"] in online:
            continue
        tried = c.execute("SELECT 1 FROM services s WHERE s.project_id=? AND NOT ("
                          + WAITING + ")", (p["id"],)).fetchone()
        if tried:
            report.projects_gone_kept.append(f"{_label(p)}: deleted online, but it has "
                                             "deployed here; kept (delete it here if wanted)")
            continue
        c.execute("SAVEPOINT gone")
        try:
            c.execute("DELETE FROM chat_messages WHERE project_id=?", (p["id"],))
            c.execute("DELETE FROM services WHERE project_id=?", (p["id"],))
            c.execute("DELETE FROM projects WHERE id=?", (p["id"],))
        except sqlite3.IntegrityError:
            c.execute("ROLLBACK TO gone")
            report.projects_gone_kept.append(f"{_label(p)}: deleted online; kept, because "
                                             "something here still refers to it")
        else:
            report.projects_removed.append(_label(p))
        c.execute("RELEASE gone")


def _import_projects(c, data, report: Report, mapped: dict[str, int], now: float):
    services: dict[str, list[dict]] = {}
    for s in data["services"]:
        services.setdefault(str(s["projectId"]), []).append(s)
    for p in data["projects"]:
        ext = str(p["id"])
        label = _label(p)
        uid = mapped.get(str(p["userId"]))
        if uid is None:
            report.projects_skipped.append(f"{label}: its owner was not brought over")
            continue
        if c.execute("SELECT 1 FROM projects WHERE external_id=?", (ext,)).fetchone():
            report.projects_before += 1
            continue
        if c.execute("SELECT 1 FROM imported_project WHERE external_id=?", (ext,)).fetchone():
            report.projects_deleted_here.append(label)       # engine.delete_project removed it
            continue
        mine = services.get(ext, [])
        problem = _project_problem(c, p, mine)
        if problem:
            report.projects_skipped.append(f"{label}: {problem}; not imported")
            continue
        created = _epoch(p["createdAt"], now)
        name = (p["name"] or "").strip() or p["slug"]
        pid = c.execute("INSERT INTO projects(user_id,name,slug,status,created_at,external_id)"
                        " VALUES(?,?,?,'new',?,?)",
                        (uid, name, p["slug"], created, ext)).lastrowid
        c.execute("INSERT INTO imported_project(external_id,imported_at) VALUES(?,?)",
                  (ext, now))
        # Primary first (the query's order), as create_project inserts them. Status
        # 'new' with no deployment is what main.startup looks for.
        for s in mine:
            c.execute("INSERT INTO services(project_id,name,slug,repo_full,branch,status,"
                      "created_at) VALUES(?,?,?,?,?,'new',?)",
                      (pid, (s["name"] or "").strip() or s["slug"], s["slug"], s["repoFull"],
                       s["branch"], created))
            report.services_new += 1
        report.projects_new += 1


def _waiting_projects(c):
    return c.execute(
        "SELECT p.id, p.name, p.slug, p.user_id, u.email FROM projects p "
        "JOIN users u ON u.id=p.user_id WHERE p.external_id IS NOT NULL AND EXISTS "
        "(SELECT 1 FROM services s WHERE s.project_id=p.id AND " + WAITING + ") "
        "ORDER BY p.id").fetchall()


def _hold_back(c, report: Report):
    """Projects main.startup will not deploy, whatever the limits: their owner has no
    GitHub connection here (theirs was not imported), or is an account made here
    whose trial has ended. Listed, so nobody is surprised."""
    for p in _waiting_projects(c):
        owner = c.execute("SELECT * FROM users WHERE id=?", (p["user_id"],)).fetchone()
        if not c.execute("SELECT 1 FROM github_connections WHERE user_id=?",
                         (p["user_id"],)).fetchone():
            report.held_back.append(f"{_label(p)} of {p['email']}: no GitHub connection here")
        elif not referrals.can_deploy(owner)[0]:
            report.held_back.append(f"{_label(p)} of {p['email']}: the account here has no "
                                    "trial left and is not paid")


def _capacity(c, report: Report):
    """What waits to deploy, against this box's limits — main.startup applies the same
    ones (metrics.check_service_count, check_deploy_quota) and leaves what does not
    fit waiting. Set /admin's limits to fit before `docker restart cx-control`."""
    waiting = c.execute("SELECT p.user_id FROM services s JOIN projects p "
                        "ON p.id=s.project_id WHERE p.external_id IS NOT NULL AND "
                        + WAITING).fetchall()
    if not waiting:
        return
    per = metrics.RAM_PER_CONTAINER_MB
    limits, usage = metrics.global_limits(), metrics.global_usage()
    room = max(0, (limits["ram_mb"] - usage["ram_reserved_mb"]) // per)
    lines = [f"capacity: {len(waiting)} imported services wait to deploy. The instance "
             f"allows {limits['services']} services (it holds {usage['services']}) and "
             f"{limits['ram_mb']}MB of RAM: {room} more can run at {per}MB each"]
    if usage["services"] > limits["services"]:
        lines.append(f"  ✗ over the instance's service limit: none of them deploys until "
                     f"/admin → instance limits allows {usage['services']}")
    elif len(waiting) > room:
        lines.append(f"  ✗ {len(waiting) - room} of them do not fit the instance's RAM "
                     "limit: they wait until /admin → instance limits is raised")
    by_owner: dict[int, int] = {}
    for w in waiting:
        by_owner[w["user_id"]] = by_owner.get(w["user_id"], 0) + 1
    for uid, n in by_owner.items():
        owner = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if owner["is_admin"]:
            continue                                     # per-account quotas skip admins
        quota = metrics.user_quota(owner)
        held = c.execute("SELECT COUNT(*) n FROM services s JOIN projects p ON "
                         "p.id=s.project_id WHERE p.user_id=?", (uid,)).fetchone()["n"]
        running = c.execute("SELECT COUNT(*) n FROM services s JOIN projects p ON "
                            "p.id=s.project_id WHERE p.user_id=? AND s.status IN "
                            "('live','deploying') AND NOT (" + WAITING + ")",
                            (uid,)).fetchone()["n"]
        fits = max(0, quota["ram_mb"] // per - running)
        if held > quota["services"]:
            lines.append(f"  ✗ {owner['email']}: {held} services, their quota allows "
                         f"{quota['services']}: none of theirs deploys until it is raised")
        elif n > fits:
            lines.append(f"  ✗ {owner['email']}: {n} to deploy, their RAM quota "
                         f"({quota['ram_mb']}MB) runs {fits}: the rest wait")
    report.capacity = lines


def apply(c: sqlite3.Connection, data: dict, report: Report, *, secret: str,
          admin_emails: set[str], now: float, link_existing: frozenset = frozenset()):
    """Writes what `fetch` read, inside the caller's transaction."""
    _copy_settings(c, data, report, secret)
    mapped = _import_users(c, data, report, admin_emails, now, link_existing)
    emails = {str(u["id"]): (u["email"] or "").strip().lower() for u in data["users"]}
    _remove_gone_connections(c, data, report, emails)   # first: a changed one comes back
    _import_connections(c, data, report, mapped, emails, now)
    _remove_gone_projects(c, data, report)          # first: it may free an address
    _import_projects(c, data, report, mapped, now)
    _hold_back(c, report)
    _capacity(c, report)
    c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) "
              "DO UPDATE SET value=excluded.value", (IMPORTED_KEY, str(now)))


def _require_schema():
    try:
        version = db.schema_version()
    except sqlite3.OperationalError:
        version = 0
    if version < NEEDS_SCHEMA:
        raise Refused(f"this database is at schema version {version}; the import needs "
                      f"{NEEDS_SCHEMA}. Start cx-control with this code once (it migrates as "
                      "it starts), then run this again.")


def addresses(value) -> frozenset:
    """--link-existing's addresses, as "a@x,b@y" or any iterable of them, lowercased."""
    if isinstance(value, bool):
        raise TypeError("link_existing takes the addresses to merge, not a yes or no")
    items = value.split(",") if isinstance(value, str) else value
    found = frozenset(e.strip().lower() for e in items if e and e.strip())
    bad = sorted(e for e in found if "@" not in e)
    if bad:
        raise Refused(f"--link-existing takes email addresses, a@x,b@y: not {', '.join(bad)}")
    return found


def run(*, dry_run: bool = False, link_existing=(), env=None) -> Report:
    env = os.environ if env is None else env
    link_existing = addresses(link_existing)
    _require_schema()
    names = ("TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN", "BETTER_AUTH_SECRET")
    url, token, secret = ((env.get(n) or "").strip() for n in names)
    missing = [n for n, v in zip(names, (url, token, secret)) if not v]
    if missing:
        raise Refused(f"{', '.join(missing)} not set in cx-control: put the values Vercel uses "
                      "in platform/.env, then `docker compose up -d`")
    admin_emails = {e.strip().lower() for e in (env.get("ADMIN_EMAILS") or "").split(",")
                    if e.strip()}
    data = fetch(turso_http_url(url), token)          # all reads before any write
    report = Report(dry_run=dry_run, link_existing=link_existing,
                    started=bool(db.setting(START_KEY)),
                    admin_emails_set=bool(admin_emails), mail_on=mailer.available())
    with _write_transaction(db.conn(), commit=not dry_run):
        apply(db.conn(), data, report, secret=secret, admin_emails=admin_emails,
              now=db.now(), link_existing=link_existing)
    return report


# ---------- letting them deploy (--start) ----------

@dataclass
class Start:
    dry_run: bool = False
    since: str = ""                 # when --start ran before, if it did
    waiting: int = 0                # imported services that have not started here
    held_back: list[str] = field(default_factory=list)
    capacity: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.dry_run:
            out = ["DRY RUN, nothing was recorded. --start would let the imported projects "
                   "deploy:"]
        elif self.since:
            out = [f"--start already ran here ({self.since}); imported projects deploy as "
                   "cx-control starts."]
        else:
            out = ["Recorded: imported projects deploy from the next start of cx-control on."]
        out.append(f"  {self.waiting} imported service(s) wait to deploy")
        if self.held_back:
            out.append("  held back whatever the limits (a restart after the change starts "
                       "them):")
            out += [f"    ✗ {s}" for s in self.held_back]
        out += [f"  {line}" for line in self.capacity]
        if not self.dry_run:
            out.append("  next: `docker restart cx-control && docker logs -f cx-control`, then "
                       "--notify")
        return "\n".join(out)


def start(*, dry_run: bool = False) -> Start:
    """Records that the imported projects may deploy (START_KEY). Run once app.<domain>
    is here and verify-public.sh passed: main.startup deploys none before it, however
    often cx-control restarts on the way there."""
    _require_schema()
    if not db.setting(IMPORTED_KEY):
        raise Refused("nothing has been imported here yet: run the import first "
                      "(docs/RUNBOOK.md, Leaving Vercel, step 4)")
    since = db.setting(START_KEY)
    result = Start(dry_run=dry_run, since=_day(float(since)) if since else "")
    c = db.conn()
    report = Report()
    _hold_back(c, report)
    _capacity(c, report)
    result.held_back, result.capacity = report.held_back, report.capacity
    result.waiting = c.execute("SELECT COUNT(*) n FROM services s JOIN projects p ON "
                               "p.id=s.project_id WHERE p.external_id IS NOT NULL AND "
                               + WAITING).fetchone()["n"]
    if not dry_run and not since:
        db.q("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING",
             (START_KEY, str(db.now())))
    return result


# ---------- telling people (--notify) ----------

def send_deploying(to: str, projects: list[tuple[str, str]]) -> bool:
    login = f"{engine.BASE_URL}/login"
    listed = "\n".join(f"  {name}  {engine.service_url(slug)}" for name, slug in projects)
    forgot = "Forgot it? \"Forgot your password?\" on that page sends you a new one."
    text = (f"Cicatrixa can deploy now, and the projects you planned have started deploying:"
            f"\n\n{listed}\n\nEach project's page shows its deploy log. Sign in at {login} "
            f"with your Cicatrixa email and password. {forgot}")
    rows = "".join(
        f'<div style="margin:0 0 8px"><b>{html.escape(name)}</b> '
        f'<span style="color:#74857B">{html.escape(engine.service_url(slug))}</span></div>'
        for name, slug in projects)
    body = f"""
<p style="font-size:14.5px;line-height:1.6;color:#E4EEE7;margin:0 0 18px">
Cicatrixa can deploy now, and the projects you planned have started deploying:</p>
<div style="font-size:13px;margin:0 0 22px">{rows}</div>
<a href="{html.escape(login)}" style="display:inline-block;background:#40D967;color:#06130A;
text-decoration:none;padding:11px 22px;border-radius:8px;font-size:12.5px;font-weight:600;
letter-spacing:.04em">Watch them deploy →</a>
<p style="font-size:12.5px;color:#74857B;margin:18px 0 0">Sign in with your Cicatrixa email
and password. {html.escape(forgot)}</p>"""
    return mailer.send(to, "Deploying is open: your Cicatrixa projects are deploying",
                       text, mailer._wrap("deploying is open", body))


@dataclass
class Notice:
    dry_run: bool = False
    owed: list[str] = field(default_factory=list)      # "email: project, project"
    sent: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    not_started: int = 0
    stopped: bool = False
    last_error: str = ""

    def summary(self) -> str:
        if self.dry_run:
            out = [f"DRY RUN, nobody was emailed. {len(self.owed)} would be:"]
            out += [f"  - {o}" for o in self.owed]
        else:
            out = [f"emailed {len(self.sent)} of {len(self.owed)}"]
            out += [f"  ✓ {e}" for e in self.sent]
            out += [f"  ✗ {e}" for e in self.failed]
            if self.failed:
                out.append(f"  the last failure: {self.last_error}")
            if self.stopped:
                out.append(f"  stopped after {FAILURES_IN_A_ROW} failures in a row (Resend's "
                           "daily limit answers like this): run --notify again later")
            elif self.failed:
                out.append("  run --notify again later: it sends only what is still owed")
        if self.not_started:
            out.append(f"  {self.not_started} imported project(s) have not started deploying "
                       "yet; a later --notify tells their owners once they have")
        return "\n".join(out)


def _owed() -> list[tuple[dict, list]]:
    """(owner, projects) owed a 'deploying' email: imported projects nobody was told
    about yet, at least one of whose services has started here (main.startup admitted
    it, or it has a deployment), of owners still allowed to deploy."""
    rows = db.all_(
        "SELECT p.id, p.name, p.slug, p.user_id FROM projects p "
        "WHERE p.external_id IS NOT NULL AND p.notified_at IS NULL AND EXISTS "
        "(SELECT 1 FROM services s WHERE s.project_id=p.id AND " + STARTED + ") "
        "ORDER BY p.user_id, p.id")
    owed: dict[int, tuple[dict, list]] = {}
    for p in rows:
        owner = db.one("SELECT * FROM users WHERE id=?", (p["user_id"],))
        if owner and referrals.can_deploy(owner)[0]:
            owed.setdefault(owner["id"], (owner, []))[1].append(p)
    return list(owed.values())


def notify(*, dry_run: bool = False, sleep=time.sleep) -> Notice:
    """Email each owner whose imported projects have started deploying. A project is
    marked once its email went out, so a send that failed is tried again next run, and
    nobody hears about the same project twice."""
    _require_schema()
    if not dry_run and not mailer.available():
        raise Refused("RESEND_API_KEY is not set here, so nobody can be emailed. Put "
                      "Vercel's RESEND_API_KEY and MAIL_FROM in platform/.env, `docker "
                      "compose up -d`, then run --notify again: it sends what is owed.")
    notice = Notice(dry_run=dry_run)
    in_a_row = 0
    for owner, projects in _owed():
        notice.owed.append(f"{owner['email']}: {', '.join(p['name'] for p in projects)}")
        if dry_run:
            continue
        if notice.sent or notice.failed:
            sleep(SEND_INTERVAL)
        if send_deploying(owner["email"], [(p["name"], p["slug"]) for p in projects]):
            db.q(f"UPDATE projects SET notified_at=? WHERE id IN "
                 f"({','.join('?' * len(projects))})", (db.now(), *(p["id"] for p in projects)))
            notice.sent.append(owner["email"])
            in_a_row = 0
            continue
        notice.failed.append(owner["email"])
        notice.last_error = mailer.last_error
        in_a_row += 1
        if in_a_row >= FAILURES_IN_A_ROW:
            notice.stopped = True
            break
    notice.not_started = db.one(
        "SELECT COUNT(*) n FROM projects p WHERE p.external_id IS NOT NULL AND "
        "p.notified_at IS NULL AND NOT EXISTS (SELECT 1 FROM services s WHERE "
        "s.project_id=p.id AND " + STARTED + ")")["n"]
    return notice


# ---------- the command ----------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.import_online",
        description="Copy the online workspace's users, GitHub App, connections and planned "
                    "projects into this box (docs/RUNBOOK.md, \"Leaving Vercel\").")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be done, and write or send nothing")
    parser.add_argument("--link-existing", metavar="EMAILS", default="",
                        help="merge the online accounts with these addresses (a@x,b@y) into "
                             "the accounts already here with the same address, when both "
                             "confirmed it; only those named (check the dry run first)")
    parser.add_argument("--start", action="store_true",
                        help="import nothing: record that app.<domain> is here and verified, "
                             "so cx-control's next start deploys the imported projects")
    parser.add_argument("--notify", action="store_true",
                        help="import nothing: email the owners whose imported projects have "
                             "started deploying and were not told yet")
    args = parser.parse_args(argv)
    if args.notify and args.start:
        parser.error("--start and --notify are separate steps: --start, restart, then --notify")
    if (args.notify or args.start) and args.link_existing:
        parser.error("--link-existing belongs to the import, not to --start or --notify")
    # The live database as it is: no schema work from a side door, and no empty
    # database conjured up at a wrong path (as app.promote).
    if not os.path.exists(db.DB_PATH):
        print(f"no database at {db.DB_PATH}: run this inside cx-control", file=sys.stderr)
        return 1
    try:
        if args.notify:
            print(notify(dry_run=args.dry_run).summary())
        elif args.start:
            print(start(dry_run=args.dry_run).summary())
        else:
            print(run(dry_run=args.dry_run, link_existing=args.link_existing).summary())
    except Refused as exc:
        what = ("notify refused, nobody was emailed" if args.notify
                else "start refused, nothing was recorded" if args.start
                else "import refused, nothing was written")
        print(f"{what}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
