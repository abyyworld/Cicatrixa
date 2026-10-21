"""Bring the online workspace onto this box, once it runs Docker:

    docker exec cx-control python -m app.import_online --dry-run
    docker exec cx-control python -m app.import_online --notify

Until the platform had a Docker host, app.cicatrixa.com ran on Vercel (accounts/,
workspace/): people signed up, connected GitHub through the same GitHub App, and
planned projects nothing could build yet. This copies all of it out of that Turso
database: every user (Better Auth's password hashes work here unchanged — see
auth.verify_password), the GitHub App's credentials, every GitHub connection and every
planned project. The next start of cx-control deploys those projects (main.startup).
docs/RUNBOOK.md, "Leaving Vercel", has the order to do it in.

Safe to run again: what it brought over carries the workspace's own id (external_id),
so a second run only adds what is new. Everything is written in one transaction;
--dry-run rolls it back after printing what it would have done.

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
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import auth, db, engine, mailer

NEEDS_SCHEMA = 3            # db.MIGRATIONS 3 adds the external_id columns
ENC_PREFIX = "enc1:"

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


class Refused(Exception):
    """The import cannot run as things stand. The message says why, for the operator."""


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
    admin_emails_set: bool = True
    app: str = "none online: nothing copied"
    users_new: int = 0
    users_before: int = 0
    users_linked: list[str] = field(default_factory=list)
    no_password: list[str] = field(default_factory=list)
    admins: list[str] = field(default_factory=list)
    users_skipped: list[str] = field(default_factory=list)
    connections_new: int = 0
    connections_before: int = 0
    connections_skipped: list[str] = field(default_factory=list)
    projects_new: int = 0
    services_new: int = 0
    projects_before: int = 0
    projects_skipped: list[str] = field(default_factory=list)
    # box user id -> {"email", "no_password", "projects": [(name, slug)]}, for --notify:
    # only projects this run brought over, so a second run emails nobody twice.
    to_notify: dict[int, dict] = field(default_factory=dict)
    mail: str = ""

    def summary(self) -> str:
        out = [("DRY RUN, nothing was written. " if self.dry_run else "")
               + f"Online workspace -> {db.DB_PATH}",
               f"  GitHub App: {self.app}",
               f"  users: {self.users_new} new, {len(self.users_linked)} matched an account "
               f"already here (it keeps its own password), {self.users_before} imported before"]
        out += [f"    = {e}" for e in self.users_linked]
        out += [f"    ✗ {s}" for s in self.users_skipped]
        if self.no_password:
            out.append(f"  {len(self.no_password)} with no password online: they choose one "
                       "with \"Forgot your password?\" here")
            out += [f"    - {e}" for e in self.no_password]
        admins = ", ".join(self.admins) or "none"
        if not self.admin_emails_set:
            admins += (" (ADMIN_EMAILS is empty here: make yours with "
                       "`docker exec cx-control python -m app.promote you@example.com`)")
        out.append(f"  new admins: {admins}")
        out.append(f"  GitHub connections: {self.connections_new} new, "
                   f"{self.connections_before} imported before")
        out += [f"    ✗ {s}" for s in self.connections_skipped]
        out.append(f"  projects: {self.projects_new} new ({self.services_new} services), "
                   f"{self.projects_before} imported before")
        out += [f"    ✗ {s}" for s in self.projects_skipped]
        if self.mail:
            out.append(f"  email: {self.mail}")
        if self.projects_new and not self.dry_run:
            out.append("  next: `docker restart cx-control` deploys the new projects as it "
                       "starts")
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


def _import_users(c, data, report: Report, admin_emails: set[str], now: float):
    """Returns {online user id: user id here} for everyone who arrived or was here."""
    passwords = {str(r["userId"]): r["password"] for r in data["credentials"]}
    mapped: dict[str, int] = {}
    for u in data["users"]:
        ext = str(u["id"] or "")
        email = (u["email"] or "").strip().lower()
        if not ext or "@" not in email:
            report.users_skipped.append(f"online user {ext or '?'}: no usable email")
            continue
        row = c.execute("SELECT id FROM users WHERE external_id=?", (ext,)).fetchone()
        if row:
            mapped[ext] = row["id"]
            report.users_before += 1
            continue
        row = c.execute("SELECT id, external_id FROM users WHERE email=?", (email,)).fetchone()
        if row:
            if row["external_id"]:
                report.users_skipped.append(f"{email}: the account here is already linked "
                                            "to another online account")
                continue
            # Their account here stays as it is, password included; it only learns
            # which online account it is, so the projects and connection find it.
            c.execute("UPDATE users SET external_id=? WHERE id=?", (ext, row["id"]))
            mapped[ext] = row["id"]
            report.users_linked.append(email)
            continue
        verified = _truthy(u["emailVerified"])
        pw_hash = passwords.get(ext)
        if not (isinstance(pw_hash, str) and _BETTER_AUTH_HASH.fullmatch(pw_hash)):
            # Only email sign-in exists online, so this should not happen; if it does,
            # a password nobody knows, and "Forgot your password?" sets a real one.
            pw_hash = auth.hash_password(secrets.token_urlsafe(32))
            report.no_password.append(email)
        # The emailed link they clicked online is the proof ADMIN_EMAILS relies on
        # (main.verify_code_submit asks the same of a code entered here).
        admin = verified and email in admin_emails
        # created_at is now, not their online sign-up: the 14-day trial is for
        # deploying, which starts today.
        mapped[ext] = c.execute(
            "INSERT INTO users(email,pw_hash,is_admin,email_verified,created_at,external_id)"
            " VALUES(?,?,?,?,?,?)",
            (email, pw_hash, 1 if admin else 0, 1 if verified else 0, now, ext)).lastrowid
        report.users_new += 1
        if admin:
            report.admins.append(email)
    return mapped


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
                  "created_at) VALUES(?,?,?,?,?)",
                  (uid, "app", iid, g["accountLogin"] or "", now))
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


def _import_projects(c, data, report: Report, mapped: dict[str, int],
                     emails: dict[str, str], no_password: set[str], now: float):
    services: dict[str, list[dict]] = {}
    for s in data["services"]:
        services.setdefault(str(s["projectId"]), []).append(s)
    for p in data["projects"]:
        ext = str(p["id"])
        label = f"{p['name']} ({p['slug']})"
        uid = mapped.get(str(p["userId"]))
        if uid is None:
            report.projects_skipped.append(f"{label}: its owner was not imported")
            continue
        if c.execute("SELECT 1 FROM projects WHERE external_id=?", (ext,)).fetchone():
            report.projects_before += 1
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
        # Primary first (the query's order), as create_project inserts them. Status
        # 'new' with no deployment is what main.startup looks for.
        for s in mine:
            c.execute("INSERT INTO services(project_id,name,slug,repo_full,branch,status,"
                      "created_at) VALUES(?,?,?,?,?,'new',?)",
                      (pid, (s["name"] or "").strip() or s["slug"], s["slug"], s["repoFull"],
                       s["branch"], created))
            report.services_new += 1
        report.projects_new += 1
        email = emails[str(p["userId"])]
        entry = report.to_notify.setdefault(uid, {"email": email, "projects": [],
                                                  "no_password": email in no_password})
        entry["projects"].append((name, p["slug"]))


def apply(c: sqlite3.Connection, data: dict, report: Report, *, secret: str,
          admin_emails: set[str], now: float):
    """Writes what `fetch` read, inside the caller's transaction."""
    _copy_settings(c, data, report, secret)
    mapped = _import_users(c, data, report, admin_emails, now)
    emails = {str(u["id"]): (u["email"] or "").strip().lower() for u in data["users"]}
    _import_connections(c, data, report, mapped, emails, now)
    _import_projects(c, data, report, mapped, emails, set(report.no_password), now)


# ---------- telling people ----------

def send_deploying(to: str, projects: list[tuple[str, str]], no_password: bool) -> bool:
    login = f"{engine.BASE_URL}/login"
    listed = "\n".join(f"  {name}  {engine.service_url(slug)}" for name, slug in projects)
    reset = (" Your account has no password here yet: choose one with \"Forgot your "
             "password?\" on that page." if no_password else "")
    text = (f"Cicatrixa can deploy now. The projects you planned are being deployed:\n\n"
            f"{listed}\n\nEach project's page shows its deploy log as it runs. Sign in at "
            f"{login} with your Cicatrixa email and password.{reset}")
    rows = "".join(
        f'<div style="margin:0 0 8px"><b>{html.escape(name)}</b> '
        f'<span style="color:#74857B">{html.escape(engine.service_url(slug))}</span></div>'
        for name, slug in projects)
    body = f"""
<p style="font-size:14.5px;line-height:1.6;color:#E4EEE7;margin:0 0 18px">
Cicatrixa can deploy now. The projects you planned are being deployed:</p>
<div style="font-size:13px;margin:0 0 22px">{rows}</div>
<a href="{html.escape(login)}" style="display:inline-block;background:#40D967;color:#06130A;
text-decoration:none;padding:11px 22px;border-radius:8px;font-size:12.5px;font-weight:600;
letter-spacing:.04em">Watch them deploy →</a>
<p style="font-size:12.5px;color:#74857B;margin:18px 0 0">Sign in with your Cicatrixa email
and password.{html.escape(reset)}</p>"""
    return mailer.send(to, "Deploying is open: your Cicatrixa projects are on their way",
                       text, mailer._wrap("deploying is open", body))


def _people(n: int) -> str:
    return f"{n} {'person' if n == 1 else 'people'}"


def _notify(report: Report):
    people = list(report.to_notify.values())
    if report.dry_run:
        report.mail = f"would email {_people(len(people))} (never in a dry run)"
    elif not mailer.available():
        report.mail = "RESEND_API_KEY is not set here, so nobody was emailed"
    else:
        sent = sum(1 for p in people
                   if send_deploying(p["email"], p["projects"], p["no_password"]))
        report.mail = f"emailed {sent} of {_people(len(people))}" + (
            f"; the last failure: {mailer.last_error}" if sent < len(people) else "")


# ---------- the command ----------

def run(*, dry_run: bool = False, notify: bool = False, env=None) -> Report:
    env = os.environ if env is None else env
    try:
        version = db.schema_version()
    except sqlite3.OperationalError:
        version = 0
    if version < NEEDS_SCHEMA:
        raise Refused(f"this database is at schema version {version}; the import needs "
                      f"{NEEDS_SCHEMA}. Start cx-control with this code once (it migrates as "
                      "it starts), then run this again.")
    names = ("TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN", "BETTER_AUTH_SECRET")
    url, token, secret = ((env.get(n) or "").strip() for n in names)
    missing = [n for n, v in zip(names, (url, token, secret)) if not v]
    if missing:
        raise Refused(f"{', '.join(missing)} not set in cx-control: put the values Vercel uses "
                      "in platform/.env, then `docker compose up -d`")
    admin_emails = {e.strip().lower() for e in (env.get("ADMIN_EMAILS") or "").split(",")
                    if e.strip()}
    data = fetch(turso_http_url(url), token)          # all reads before any write
    report = Report(dry_run=dry_run, admin_emails_set=bool(admin_emails))
    with _write_transaction(db.conn(), commit=not dry_run):
        apply(db.conn(), data, report, secret=secret, admin_emails=admin_emails,
              now=db.now())
    if notify:
        _notify(report)                               # only after the commit
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.import_online",
        description="Copy the online workspace's users, GitHub App, connections and planned "
                    "projects into this box (docs/RUNBOOK.md, \"Leaving Vercel\").")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be imported and write nothing")
    parser.add_argument("--notify", action="store_true",
                        help="email each person whose projects this run imports")
    args = parser.parse_args(argv)
    # The live database as it is: no schema work from a side door, and no empty
    # database conjured up at a wrong path (as app.promote).
    if not os.path.exists(db.DB_PATH):
        print(f"no database at {db.DB_PATH}: run this inside cx-control", file=sys.stderr)
        return 1
    try:
        report = run(dry_run=args.dry_run, notify=args.notify)
    except Refused as exc:
        print(f"import refused, nothing was written: {exc}", file=sys.stderr)
        return 1
    print(report.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
