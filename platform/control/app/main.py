"""Cicatrixa platform control plane — web UI + API + orchestration."""
import asyncio
import hmac
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

import docker.errors
from fastapi import FastAPI, Form, Request
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               Response, StreamingResponse)
from fastapi.templating import Jinja2Templates

from . import (ai, auth, billing, bus, db, dbprovision, engine, flywheel, gh,
               invites, mailer, medic, metrics, referrals, watchdog)

BASE_DOMAIN = os.environ.get("BASE_DOMAIN", "localhost")
BASE_URL = os.environ.get("BASE_URL", f"http://{BASE_DOMAIN}")
ADMIN_EMAILS = {e.strip().lower() for e in
                os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
RESERVED_SLUGS = {"app", "www", "api", "admin", "demo", "healer", "traefik", "mail",
                  "status", "docs"}

app = FastAPI(title="Cicatrixa")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
templates.env.globals["fmt_bytes"] = metrics.fmt_bytes
templates.env.globals["ram_per_container"] = metrics.RAM_PER_CONTAINER_MB
templates.env.globals["now"] = db.now

# Password hashing and email get pools of their own. The default executor also runs
# deploys and builds, which hold a thread for minutes; a login must never queue
# behind them, and a hung mail server must never hold up a login.
_AUTH_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cx-auth")
_MAIL_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cx-mail")


async def _in(pool: ThreadPoolExecutor, fn, *args):
    return await asyncio.get_running_loop().run_in_executor(pool, fn, *args)


# ---------- helpers ----------

def current_user(request: Request):
    return auth.session_user(request.cookies.get(auth.COOKIE_NAME))


def render(request: Request, template: str, **ctx) -> HTMLResponse:
    user = ctx.pop("user", None) or current_user(request)
    connection = None
    if user:
        connection = db.one("SELECT * FROM github_connections WHERE user_id=? "
                            "ORDER BY id DESC LIMIT 1", (user["id"],))
    return templates.TemplateResponse(request, template, {
        "user": user, "connection": connection, "base_domain": BASE_DOMAIN,
        "gh_app_configured": gh.app_configured(), "gh_app_slug": gh.app_slug(),
        "ai_on": ai.available(), **ctx})


def need_login(request: Request):
    """Off to log in — unless they already are, and are just not allowed here (a
    non-admin on /admin): login would send them straight back, forever."""
    if current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    # Only a page can be come back to. The browser follows the post-login
    # redirect with a GET, and a form's POST-only URL answers that with a 405.
    if request.method == "GET":
        return RedirectResponse("/login?" + urlencode({"next": request.url.path}),
                                status_code=303)
    return RedirectResponse("/login", status_code=303)


def _safe_next(path: str) -> str:
    """Where to land after logging in: a path on this site, never another host.
    "//evil.com" and "/\\evil.com" are host-relative URLs to a browser."""
    if path.startswith("/") and not path.startswith(("//", "/\\")):
        return path
    return "/dashboard"


def _signed_in(user_id: int, to: str = "/dashboard") -> RedirectResponse:
    resp = RedirectResponse(to, status_code=303)
    resp.set_cookie(auth.COOKIE_NAME, auth.make_session(user_id), max_age=auth.SESSION_TTL,
                    httponly=True, samesite="lax", secure=engine.HTTPS_ENABLED)
    return resp


def make_slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:40] or "app"
    slug, n = base, 1
    while (slug in RESERVED_SLUGS
           or db.one("SELECT 1 FROM projects WHERE slug=?", (slug,))
           or db.one("SELECT 1 FROM services WHERE slug=?", (slug,))):
        n += 1
        slug = f"{base}-{n}"
    return slug


def own_project(request: Request, project_id: int):
    user = current_user(request)
    if not user:
        return None, None
    project = db.one("SELECT * FROM projects WHERE id=? AND user_id=?",
                     (project_id, user["id"]))
    return user, project


# ---------- lifecycle ----------

@app.on_event("startup")
async def startup():
    db.init()
    loop = asyncio.get_running_loop()
    bus.attach_loop(loop)
    watchdog.attach_loop(loop)
    try:
        engine.dock().networks.get(engine.NETWORK)
    except docker.errors.NotFound:
        engine.dock().networks.create(engine.NETWORK, driver="bridge")
    except Exception:
        pass
    # Builds run inside this container — a restart mid-build orphans the
    # deployment as "running" forever. Mark those failed and retry them.
    for o in db.all_("SELECT id, service_id FROM deployments WHERE status='running'"):
        db.q("UPDATE deployments SET status='failed', finished_at=?, "
             "log=log || char(10) || '✗ interrupted by platform restart — retrying' "
             "WHERE id=?", (db.now(), o["id"]))
        asyncio.create_task(engine.deploy(o["service_id"], trigger="retry"))
    asyncio.create_task(watchdog.run_forever())


# ---------- public pages ----------

@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    return render(request, "landing.html")


@app.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request):
    return render(request, "legal.html", page="terms",
                  page_title="Terms of Service", kicker="the deal, in plain words")


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    return render(request, "legal.html", page="privacy",
                  page_title="Privacy Policy", kicker="what we know and why")


@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request, invite: str = "", ref: str = ""):
    """Open signup — everyone gets a 14-day free trial. A ?ref= link
    additionally ties the account to its referrer for the quota bonus."""
    referrer = referrals.referrer_for(ref) if ref else None
    return render(request, "signup.html", error=None, ref_code=ref if referrer else "",
                  referrer_email=referrer["email"] if referrer else "")


@app.post("/signup")
async def signup(request: Request, email: str = Form(...), password: str = Form(...),
                 ref_code: str = Form("")):
    email = email.strip().lower()

    def fail(msg):
        return render(request, "signup.html", error=msg, ref_code=ref_code,
                      referrer_email="")

    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return fail("That doesn't look like an email.")
    if len(password) < 8:
        return fail("Password must be at least 8 characters.")
    if db.one("SELECT 1 FROM users WHERE email=?", (email,)):
        return fail("An account with that email already exists.")
    # Without ADMIN_EMAILS, the first account on a fresh instance is the admin. An
    # address in ADMIN_EMAILS is NOT made admin here: anyone can type it, and with
    # email off nothing checks they own it. It becomes admin once its emailed code
    # proves that (verify_code_submit), or by `python -m app.promote` on the server.
    is_admin = not ADMIN_EMAILS and invites.bootstrap_open()
    referrer = referrals.referrer_for(ref_code) if ref_code else None
    # scrypt is deliberately slow; on the event loop it would stall every request.
    pw_hash = await _in(_AUTH_POOL, auth.hash_password, password)
    uid = db.q("INSERT INTO users(email,pw_hash,is_admin,referred_by,created_at) "
               "VALUES(?,?,?,?,?)",
               (email, pw_hash, 1 if is_admin else 0,
                referrer["id"] if referrer else None, db.now())).lastrowid
    return await _start_verification(uid, email)


@app.get("/request-access", response_class=HTMLResponse)
async def request_access_page(request: Request):
    return render(request, "request_access.html", error=None, sent=False)


@app.post("/request-access")
async def request_access_submit(request: Request, email: str = Form(...)):
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email.strip()):
        return render(request, "request_access.html", error="That doesn't look like an email.",
                      sent=False)
    message = invites.request_access(email)
    return render(request, "request_access.html", error=None, sent=True, message=message)


# A new code at most once a minute per account, whether or not its email went
# out: signing up with somebody else's address and pressing Resend, or logging in
# over and over, would otherwise flood their inbox and spend the shared Resend
# quota — and each new code is 5 more guesses.
CODE_COOLDOWN = 60
# Wrong guesses allowed per code; the code then dies. Across codes, an account
# gets CODE_STRIKES wrong guesses an hour and then no new code until the hour is
# out: 25 guesses an hour against a million codes is years for a 50% chance.
CODE_ATTEMPTS = 5
CODE_STRIKES = 25
_code_issued_at: dict[int, float] = {}
_code_failures: dict[int, int] = {}
_code_strikes: dict[int, list[float]] = {}


def _strikes(user_id: int) -> list[float]:
    recent = [t for t in _code_strikes.get(user_id, []) if time.time() - t < 3600]
    _code_strikes[user_id] = recent
    return recent


async def _issue_code(user_id: int, email: str) -> str:
    """Email a fresh code: "sent", "failed", "cooldown" (a code was made under a
    minute ago), or "blocked" (too many wrong guesses this hour).

    Awaited, not fired and forgotten: a send that failed used to leave the person
    on a page saying "we sent a code" that was never coming, with no way to learn
    otherwise. Now the page says so, and /admin shows Resend's reason."""
    if len(_strikes(user_id)) >= CODE_STRIKES:
        return "blocked"
    if time.time() - _code_issued_at.get(user_id, 0) < CODE_COOLDOWN:
        return "cooldown"
    _code_issued_at[user_id] = time.time()     # before the await: a second request waits
    code = auth.generate_code()
    db.q("UPDATE users SET verify_code=?, verify_expires=? WHERE id=?",
         (code, db.now() + auth.CODE_TTL, user_id))
    _code_failures.pop(user_id, None)
    if not await _in(_MAIL_POOL, mailer.send_verification_code, email, code):
        return "failed"
    return "sent"


def _pending(resp: RedirectResponse, user_id: int) -> RedirectResponse:
    resp.set_cookie(auth.PENDING_COOKIE_NAME, auth.make_pending(user_id),
                    max_age=auth.PENDING_TTL, httponly=True, samesite="lax",
                    secure=engine.HTTPS_ENABLED)
    return resp


async def _start_verification(user_id: int, email: str,
                              then: str = "/dashboard") -> RedirectResponse:
    """Send a code and gate on it — or, if mail isn't configured on this instance,
    verify immediately so local/dev setups without RESEND_API_KEY still work."""
    if not mailer.available():
        db.q("UPDATE users SET email_verified=1 WHERE id=?", (user_id,))
        return _signed_in(user_id, then)
    outcome = await _issue_code(user_id, email)
    flag = {"sent": "", "cooldown": "", "failed": "?unsent=1", "blocked": "?blocked=1"}
    return _pending(RedirectResponse("/verify-code" + flag[outcome], status_code=303),
                    user_id)


def _pending_user(request: Request):
    """Who is between signup and their code — never an account that is already
    verified: a pending cookie must not become a way into one without its password."""
    user = auth.pending_user(request.cookies.get(auth.PENDING_COOKIE_NAME))
    return user if user and not user["email_verified"] else None


@app.get("/verify-code", response_class=HTMLResponse)
async def verify_code_page(request: Request):
    user = _pending_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    qp = request.query_params
    return render(request, "verify_code.html", user=None, pending_email=user["email"],
                  error=qp.get("error"), resent=qp.get("resent"), unsent=qp.get("unsent"),
                  wait=qp.get("wait"), blocked=qp.get("blocked"))


@app.post("/verify-code")
async def verify_code_submit(request: Request, code: str = Form(...)):
    user = _pending_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    valid = bool(
        user["verify_code"] and user["verify_expires"]
        and db.now() < user["verify_expires"]
        and hmac.compare_digest(code.strip(), user["verify_code"]))
    if not valid:
        error = "That code is incorrect or has expired."
        if user["verify_code"]:
            _strikes(user["id"]).append(time.time())
            failures = _code_failures.get(user["id"], 0) + 1
            _code_failures[user["id"]] = failures
            if failures >= CODE_ATTEMPTS or len(_strikes(user["id"])) >= CODE_STRIKES:
                db.q("UPDATE users SET verify_code=NULL, verify_expires=NULL WHERE id=?",
                     (user["id"],))
                _code_failures.pop(user["id"], None)
                error = "Too many wrong codes. Press Resend code for a new one."
        return render(request, "verify_code.html", user=None, pending_email=user["email"],
                      error=error)
    _code_failures.pop(user["id"], None)
    # The code proves the address is theirs — the one proof ADMIN_EMAILS can rely on.
    admin = 1 if user["is_admin"] or user["email"] in ADMIN_EMAILS else 0
    db.q("UPDATE users SET email_verified=1, verify_code=NULL, verify_expires=NULL, "
         "is_admin=? WHERE id=?", (admin, user["id"]))
    resp = _signed_in(user["id"], "/dashboard?verified=1")
    resp.delete_cookie(auth.PENDING_COOKIE_NAME)
    return resp


@app.post("/verify-code/resend")
async def verify_code_resend(request: Request):
    user = _pending_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    outcome = await _issue_code(user["id"], user["email"])
    flag = {"sent": "resent=1", "failed": "unsent=1", "cooldown": "wait=1",
            "blocked": "blocked=1"}[outcome]
    # The pending cookie is not renewed: it lasts PENDING_TTL from signup or login.
    return RedirectResponse("/verify-code?" + flag, status_code=303)


@app.post("/verify-code/cancel")
async def verify_code_cancel():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.PENDING_COOKIE_NAME)
    return resp


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = ""):
    if current_user(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return render(request, "login.html", error=None, next=next)


@app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...),
                next: str = Form("")):
    user = db.one("SELECT * FROM users WHERE email=?", (email.strip().lower(),))
    if not user or not await _in(_AUTH_POOL, auth.verify_password, password,
                                 user["pw_hash"]):
        return render(request, "login.html", error="Wrong email or password.", next=next)
    if not user["email_verified"]:
        return await _start_verification(user["id"], user["email"], _safe_next(next))
    return _signed_in(user["id"], _safe_next(next))


# ---------- forgotten passwords ----------

# One reset email per account per minute, so the form cannot be used to flood
# somebody's inbox. In memory: the control plane is a single process.
RESET_COOLDOWN = 60
_reset_sent_at: dict[int, float] = {}


def _reset_url(user) -> str:
    return f"{BASE_URL}/reset-password?" + urlencode({"token": auth.make_reset(user)})


@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return render(request, "forgot_password.html", sent=False, email="",
                  mail_on=mailer.available())


@app.post("/forgot-password", response_class=HTMLResponse)
async def forgot_password(request: Request, email: str = Form(...)):
    email = email.strip().lower()
    if not mailer.available():
        return render(request, "forgot_password.html", sent=False, email=email,
                      mail_on=False)
    user = db.one("SELECT * FROM users WHERE email=?", (email,))
    if user and time.time() - _reset_sent_at.get(user["id"], 0) >= RESET_COOLDOWN:
        _reset_sent_at[user["id"]] = time.time()
        await _in(_MAIL_POOL, mailer.send_password_reset, user["email"], _reset_url(user))
    # The same answer whether or not the account exists.
    return render(request, "forgot_password.html", sent=True, email=email, mail_on=True)


@app.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request, token: str = ""):
    user = auth.reset_user(token)
    return render(request, "reset_password.html", user=None, token=token,
                  email=user["email"] if user else "", invalid=not user, error=None)


@app.post("/reset-password", response_class=HTMLResponse)
async def reset_password(request: Request, token: str = Form(...),
                         password: str = Form(...)):
    user = auth.reset_user(token)
    if not user:
        return render(request, "reset_password.html", user=None, token="", email="",
                      invalid=True, error=None)
    if len(password) < 8:
        return render(request, "reset_password.html", user=None, token=token,
                      email=user["email"], invalid=False,
                      error="Password must be at least 8 characters.")
    pw_hash = await _in(_AUTH_POOL, auth.hash_password, password)
    # The link reached them, so the address is theirs: no code needed after this.
    # The new hash also ends every session from before it (auth.make_session).
    db.q("UPDATE users SET pw_hash=?, email_verified=1, verify_code=NULL, "
         "verify_expires=NULL WHERE id=?", (pw_hash, user["id"]))
    resp = _signed_in(user["id"], "/dashboard?password_reset=1")
    resp.delete_cookie(auth.PENDING_COOKIE_NAME)
    return resp


@app.post("/logout")
async def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


# ---------- dashboard & projects ----------

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = current_user(request)
    if not user:
        return need_login(request)
    projects = db.all_("SELECT * FROM projects WHERE user_id=? ORDER BY created_at DESC",
                       (user["id"],))
    services_by_project: dict[int, list] = {}
    for s in db.all_(
            "SELECT s.* FROM services s JOIN projects p ON p.id=s.project_id "
            "WHERE p.user_id=? ORDER BY s.id", (user["id"],)):
        services_by_project.setdefault(s["project_id"], []).append(s)
    await asyncio.to_thread(metrics.ensure_fresh)
    qp = request.query_params
    return render(request, "dashboard.html", user=user, projects=projects,
                  services_by_project=services_by_project,
                  usage=metrics.user_usage(user["id"]),
                  quota=metrics.user_quota(user),
                  invite_link=f"{BASE_URL}/signup?ref={referrals.code_for(user)}",
                  ref_stats=referrals.stats_for(user["id"]),
                  is_paid=referrals.is_paid(user),
                  trial_days_left=referrals.trial_days_left(user),
                  billing_on=billing.available(), just_paid=qp.get("paid"),
                  error=qp.get("error"), verified=qp.get("verified"),
                  password_reset=qp.get("password_reset"))


@app.get("/projects/new", response_class=HTMLResponse)
async def new_project_page(request: Request):
    user = current_user(request)
    if not user:
        return need_login(request)
    connection = db.one("SELECT * FROM github_connections WHERE user_id=? "
                        "ORDER BY id DESC LIMIT 1", (user["id"],))
    if not connection:
        return RedirectResponse("/connect/github", status_code=303)
    try:
        repos = await asyncio.to_thread(gh.list_repos, connection)
        error = request.query_params.get("error")
    except Exception as exc:
        repos, error = [], f"Could not list repositories: {exc}"
    return render(request, "new_project.html", user=user, repos=repos, error=error)


def _service_name(repo: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", repo.split("/")[-1].lower()).strip("-")[:24] or "app"


async def _add_service(pid: int, project_slug: str, connection, repo: str,
                       branch: str, primary: bool) -> int:
    name = _service_name(repo)
    slug = project_slug if primary else make_slug(f"{project_slug}-{name}")
    branch = branch or await asyncio.to_thread(gh.default_branch, connection, repo)
    return db.q("INSERT INTO services(project_id,name,slug,repo_full,branch,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (pid, name, slug, repo, branch, db.now())).lastrowid


@app.post("/projects")
async def create_project(request: Request):
    user = current_user(request)
    if not user:
        return need_login(request)
    connection = db.one("SELECT * FROM github_connections WHERE user_id=? "
                        "ORDER BY id DESC LIMIT 1", (user["id"],))
    if not connection:
        return RedirectResponse("/connect/github", status_code=303)
    ok, trial_err = referrals.can_deploy(user)
    if not ok:
        return RedirectResponse("/projects/new?error=" + trial_err.replace(" ", "+"),
                                status_code=303)
    form = await request.form()
    repos = [r for r in form.getlist("repos") if r.strip()]
    if not repos:
        return RedirectResponse("/projects/new", status_code=303)
    quota_err = metrics.check_service_count(user, extra=len(repos))
    if quota_err:
        return RedirectResponse("/projects/new?error=" + quota_err.replace(" ", "+"),
                                status_code=303)
    primary = form.get("primary") or repos[0]
    if primary in repos:  # primary service is deployed under the project subdomain
        repos.remove(primary)
        repos.insert(0, primary)
    name = (form.get("name") or "").strip() or _service_name(primary)
    branch = (form.get("branch") or "").strip()
    slug = make_slug(name)
    pid = db.q("INSERT INTO projects(user_id,name,slug,created_at) VALUES(?,?,?,?)",
               (user["id"], name, slug, db.now())).lastrowid
    for i, repo in enumerate(repos):
        await _add_service(pid, slug, connection, repo, branch, primary=(i == 0))
    asyncio.create_task(engine.deploy_project(pid, "manual"))
    return RedirectResponse(f"/projects/{pid}", status_code=303)


@app.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_page(request: Request, project_id: int):
    user, project = own_project(request, project_id)
    if not user:
        return need_login(request)
    if not project:
        return RedirectResponse("/dashboard", status_code=303)
    services = db.all_("SELECT * FROM services WHERE project_id=? ORDER BY id",
                       (project_id,))
    databases = db.all_("SELECT * FROM databases WHERE project_id=? ORDER BY id",
                        (project_id,))
    await asyncio.to_thread(metrics.ensure_fresh)
    deployments = db.all_(
        "SELECT d.id,d.sha,d.trigger,d.status,d.created_at,s.name AS service_name "
        "FROM deployments d JOIN services s ON s.id=d.service_id "
        "WHERE s.project_id=? ORDER BY d.id DESC LIMIT 15", (project_id,))
    latest = db.one(
        "SELECT d.log FROM deployments d JOIN services s ON s.id=d.service_id "
        "WHERE s.project_id=? ORDER BY d.id DESC", (project_id,))
    return render(request, "project.html", user=user, project=project,
                  services=services, databases=databases, deployments=deployments,
                  latest_log=(latest["log"] if latest else ""),
                  service_url=engine.service_url,
                  service_mem=metrics.service_mem, service_disk=metrics.service_disk,
                  connection_url=dbprovision.connection_url,
                  chat=medic.history(project_id),
                  error=request.query_params.get("error"))


@app.post("/projects/{project_id}/chat")
async def chat_send(request: Request, project_id: int, message: str = Form(...)):
    user, project = own_project(request, project_id)
    if not project:
        return JSONResponse({"ok": False}, status_code=403)
    text = message.strip()[:2000]
    if text:
        medic.add_message(project_id, "user", text)
        asyncio.create_task(asyncio.to_thread(medic.investigate_sync, project_id, text))
    return JSONResponse({"ok": True})


@app.post("/projects/{project_id}/chat/{message_id}/apply")
async def chat_apply(request: Request, project_id: int, message_id: int):
    user, project = own_project(request, project_id)
    if not project:
        return need_login(request)
    # In the background: cloning, building and testing can outlast the ~100 s a
    # request may take behind Cloudflare (tunnel mode), which would show a 524 page
    # while the work went on. Progress reaches the chat over the event stream.
    if message_id not in _applying:
        _applying.add(message_id)
        asyncio.create_task(_apply_fix(project_id, message_id))
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


_applying: set[int] = set()   # one Apply at a time per proposal: a second click waits


async def _apply_fix(project_id: int, message_id: int):
    try:
        service_id, text = await asyncio.to_thread(medic.apply_fix_sync,
                                                   project_id, message_id)
        if service_id:
            await engine.deploy(service_id, "chat-fix")
        else:
            medic.add_message(project_id, "agent", f"✖ {text}", kind="status")
    except Exception as exc:
        # Nobody is waiting on a page for this any more: say it in the chat.
        medic.add_message(project_id, "agent", f"✖ Applying the fix failed: {exc}",
                          kind="status")
    finally:
        _applying.discard(message_id)


@app.post("/projects/{project_id}/deploy")
async def redeploy(request: Request, project_id: int):
    user, project = own_project(request, project_id)
    if not project:
        return need_login(request)
    ok, trial_err = referrals.can_deploy(user)
    if not ok:
        return RedirectResponse(f"/projects/{project_id}?error="
                                + trial_err.replace(" ", "+"), status_code=303)
    asyncio.create_task(engine.deploy_project(project_id, "manual"))
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


def _own_service(request: Request, service_id: int):
    user = current_user(request)
    if not user:
        return None, None
    service = db.one(
        "SELECT s.* FROM services s JOIN projects p ON p.id=s.project_id "
        "WHERE s.id=? AND p.user_id=?", (service_id, user["id"]))
    return user, service


@app.post("/services/{service_id}/deploy")
async def redeploy_service(request: Request, service_id: int):
    user, service = _own_service(request, service_id)
    if not service:
        return need_login(request)
    ok, trial_err = referrals.can_deploy(user)
    if not ok:
        return RedirectResponse(f"/projects/{service['project_id']}?error="
                                + trial_err.replace(" ", "+"), status_code=303)
    asyncio.create_task(engine.deploy(service_id, "manual"))
    return RedirectResponse(f"/projects/{service['project_id']}", status_code=303)


@app.post("/services/{service_id}/delete")
async def delete_service(request: Request, service_id: int):
    user, service = _own_service(request, service_id)
    if not service:
        return need_login(request)
    await asyncio.to_thread(engine.delete_service, service)
    return RedirectResponse(f"/projects/{service['project_id']}", status_code=303)


@app.post("/projects/{project_id}/services")
async def add_service(request: Request, project_id: int, repo: str = Form(...),
                      branch: str = Form("")):
    user, project = own_project(request, project_id)
    if not project:
        return need_login(request)
    connection = db.one("SELECT * FROM github_connections WHERE user_id=? "
                        "ORDER BY id DESC LIMIT 1", (user["id"],))
    if not connection:
        return RedirectResponse("/connect/github", status_code=303)
    ok, trial_err = referrals.can_deploy(user)
    if not ok:
        return RedirectResponse(f"/projects/{project_id}?error="
                                + trial_err.replace(" ", "+"), status_code=303)
    quota_err = metrics.check_service_count(user, extra=1)
    if quota_err:
        return RedirectResponse(f"/projects/{project_id}?error="
                                + quota_err.replace(" ", "+"), status_code=303)
    sid = await _add_service(project_id, project["slug"], connection, repo,
                             branch.strip(), primary=False)
    asyncio.create_task(engine.deploy(sid, "manual"))
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/databases")
async def add_database(request: Request, project_id: int, name: str = Form("primary")):
    user, project = own_project(request, project_id)
    if not project:
        return need_login(request)
    quota_err = metrics.check_database_count(user, extra=1)
    if quota_err:
        return RedirectResponse(f"/projects/{project_id}?error="
                                + quota_err.replace(" ", "+"), status_code=303)
    name = re.sub(r"[^a-zA-Z0-9_-]+", "", name.strip()) or "primary"
    # In the background, like deploys: the first one pulls postgres, which on a
    # slow line outlasts what a request may take behind Cloudflare. Progress goes
    # to the project's log stream.
    asyncio.create_task(asyncio.to_thread(dbprovision.create, project, name))
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


def _own_database(request: Request, database_id: int):
    user = current_user(request)
    if not user:
        return None, None
    database = db.one(
        "SELECT d.* FROM databases d JOIN projects p ON p.id=d.project_id "
        "WHERE d.id=? AND p.user_id=?", (database_id, user["id"]))
    return user, database


@app.post("/databases/{database_id}/restart")
async def restart_database(request: Request, database_id: int):
    user, database = _own_database(request, database_id)
    if not database:
        return need_login(request)
    await asyncio.to_thread(dbprovision.restart, database_id)
    return RedirectResponse(f"/projects/{database['project_id']}", status_code=303)


@app.post("/databases/{database_id}/delete")
async def delete_database(request: Request, database_id: int):
    user, database = _own_database(request, database_id)
    if not database:
        return need_login(request)
    await asyncio.to_thread(dbprovision.delete, database_id)
    return RedirectResponse(f"/projects/{database['project_id']}", status_code=303)


@app.post("/projects/{project_id}/stop")
async def stop(request: Request, project_id: int):
    user, project = own_project(request, project_id)
    if not project:
        return need_login(request)
    await asyncio.to_thread(engine.stop_project, project)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/delete")
async def delete(request: Request, project_id: int):
    user, project = own_project(request, project_id)
    if not project:
        return need_login(request)
    form = await request.form()
    revoke = form.get("revoke", "1") != "0"
    await asyncio.to_thread(engine.delete_project, project, revoke_github=revoke)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/projects/{project_id}/autodeploy")
async def toggle_autodeploy(request: Request, project_id: int):
    user, project = own_project(request, project_id)
    if not project:
        return need_login(request)
    db.q("UPDATE projects SET autodeploy=? WHERE id=?",
         (0 if project["autodeploy"] else 1, project_id))
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.get("/projects/{project_id}/events")
async def project_events(request: Request, project_id: int):
    user, project = own_project(request, project_id)
    if not project:
        return JSONResponse({"error": "not found"}, status_code=404)
    async def alive() -> bool:
        # Still there, and still signed in: a password reset ends open streams too.
        return (not await request.is_disconnected()
                and current_user(request) is not None)

    return StreamingResponse(bus.subscribe(f"project:{project_id}", alive),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------- GitHub connect ----------

@app.get("/connect/github", response_class=HTMLResponse)
async def connect_github(request: Request):
    user = current_user(request)
    if not user:
        return need_login(request)
    return render(request, "connect.html", user=user,
                  error=request.query_params.get("error"))


@app.get("/connect/github/start")
async def connect_start(request: Request):
    user = current_user(request)
    if not user:
        return need_login(request)
    if not gh.app_configured():
        return RedirectResponse("/connect/github", status_code=303)
    existing = db.one("SELECT * FROM github_connections WHERE user_id=? AND kind='app' "
                      "ORDER BY id DESC LIMIT 1", (user["id"],))
    if existing and existing["installation_id"]:
        # App already installed — send to GitHub's installation settings page
        # (has repo picker + Save button) rather than the fresh-install page which
        # has no proceed button when the app is already installed.
        return RedirectResponse(
            f"https://github.com/settings/installations/{existing['installation_id']}")
    state = auth.make_gh_state(user["id"])
    return RedirectResponse(
        f"https://github.com/apps/{gh.app_slug()}/installations/new?state={state}")


@app.get("/connect/github/authorize")
async def connect_authorize(request: Request):
    """For an installation that already exists: GitHub's OAuth page, which comes
    back to the callback with a code, so connect_setup can see what is theirs."""
    user = current_user(request)
    if not user:
        return need_login(request)
    url = gh.authorize_url(auth.make_gh_state(user["id"]), BASE_URL)
    if not url:
        return RedirectResponse("/connect/github", status_code=303)
    return RedirectResponse(url)


@app.get("/connect/github/setup")
@app.get("/connect/github/callback")
async def connect_setup(request: Request):
    """GitHub sends the person back here after installing the App, or after
    authorizing it (/connect/github/authorize).

    Nothing in this URL can be taken on trust. installation_id can be typed, and
    the ids are sequential; binding someone else's hands over their private repos.
    So:
      - the state must be one this same signed-in account started (connect_start
        or connect_authorize) — a link from elsewhere cannot bind anything to the
        person who clicks it, nor their installation to whoever made the link;
      - GitHub's OAuth code must show the installation is on an account they
        control: their own, or an organisation they administer (seeing it is not
        enough — gh.installations_user_controls);
      - one already connected to another account is refused."""
    params = request.query_params
    user = current_user(request)
    if not user:
        return RedirectResponse("/login?" + urlencode({"next": "/connect/github"}),
                                status_code=303)

    def refuse(why: str):
        return RedirectResponse("/connect/github?" + urlencode({"error": why}),
                                status_code=303)

    if auth.gh_state_user_id(params.get("state")) != user["id"]:
        return refuse("That GitHub link was not started from this account. "
                      "Connect from this page.")
    try:
        wanted = int(params["installation_id"]) if params.get("installation_id") else None
    except ValueError:
        wanted = None
    if wanted and db.one("SELECT 1 FROM github_connections WHERE kind='app' "
                         "AND installation_id=? AND user_id=?", (wanted, user["id"])):
        return RedirectResponse("/projects/new", status_code=303)   # already theirs
    if not params.get("code"):
        return refuse("GitHub did not send an authorization. If the App is already "
                      "installed, use \"Already installed? Connect it\" below.")
    controlled = await asyncio.to_thread(gh.installations_user_controls, params["code"])
    if controlled is None:
        return refuse("GitHub did not accept the authorization. Try again.")

    def bound_elsewhere(iid: int) -> bool:
        return bool(db.one("SELECT 1 FROM github_connections WHERE kind='app' "
                           "AND installation_id=? AND user_id<>?", (iid, user["id"])))

    if wanted:
        if wanted not in controlled:
            return refuse("That installation is not on your own GitHub account or an "
                          "organisation you administer. Ask an owner to connect it.")
        chosen = wanted
    else:
        free = [i for i in controlled if not bound_elsewhere(i)]
        if not free:
            return refuse("The App is not installed on your GitHub account or on an "
                          "organisation you administer. Install it first.")
        chosen = max(free, key=lambda i: controlled[i].get("created_at") or "")
    if bound_elsewhere(chosen):
        return refuse("That GitHub installation is already connected to another account.")
    db.q("DELETE FROM github_connections WHERE user_id=? AND kind='app'", (user["id"],))
    db.q("INSERT INTO github_connections(user_id,kind,installation_id,gh_login,created_at)"
         " VALUES(?,?,?,?,?)", (user["id"], "app", chosen,
                                (controlled[chosen].get("account") or {}).get("login", ""),
                                db.now()))
    return RedirectResponse("/projects/new", status_code=303)


@app.post("/connect/github/pat")
async def connect_pat(request: Request, token: str = Form(...)):
    user = current_user(request)
    if not user:
        return need_login(request)
    token = token.strip()
    login = await asyncio.to_thread(gh.viewer_login, token)
    if not login:
        return render(request, "connect.html", user=user,
                      error="GitHub rejected that token. It needs `repo` scope "
                            "(classic) or Contents:read + Metadata (fine-grained).")
    db.q("DELETE FROM github_connections WHERE user_id=? AND kind='pat'", (user["id"],))
    db.q("INSERT INTO github_connections(user_id,kind,pat_token,gh_login,created_at) "
         "VALUES(?,?,?,?,?)", (user["id"], "pat", token, login, db.now()))
    return RedirectResponse("/projects/new", status_code=303)


@app.post("/connect/github/disconnect")
async def disconnect(request: Request):
    user = current_user(request)
    if not user:
        return need_login(request)
    form = await request.form()
    revoke = form.get("revoke", "0") == "1"
    if revoke and gh.app_configured():
        conn = db.one("SELECT * FROM github_connections WHERE user_id=? AND kind='app' "
                      "ORDER BY id DESC LIMIT 1", (user["id"],))
        if conn and conn["installation_id"]:
            await asyncio.to_thread(gh.revoke_installation, conn["installation_id"])
    db.q("DELETE FROM github_connections WHERE user_id=?", (user["id"],))
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/api/repos")
async def api_repos(request: Request):
    """Returns the current GitHub repo list as JSON — used for polling after install."""
    user = current_user(request)
    if not user:
        return JSONResponse([])
    connection = db.one("SELECT * FROM github_connections WHERE user_id=? "
                        "ORDER BY id DESC LIMIT 1", (user["id"],))
    if not connection:
        return JSONResponse([])
    try:
        repos = await asyncio.to_thread(gh.list_repos, connection)
        return JSONResponse(repos)
    except Exception:
        return JSONResponse([])


# ---------- admin: one-click GitHub App creation (manifest flow) ----------

async def _render_admin(request: Request, user, **extra) -> HTMLResponse:
    manifest = gh.build_manifest(BASE_URL)
    await asyncio.to_thread(metrics.ensure_fresh)
    return render(request, "admin.html", user=user,
                  manifest_json=json.dumps(manifest, indent=2), base_url=BASE_URL,
                  host=metrics.host, users_usage=metrics.all_users_usage(),
                  defaults=metrics.universal_quota(),
                  global_limits=metrics.global_limits(),
                  global_usage=metrics.global_usage(),
                  growth=metrics.growth_stats(),
                  pending_requests=invites.pending_requests(),
                  sent_invites=invites.sent_invites(),
                  mail_on=mailer.available(), mail_error=mailer.last_error,
                  error=request.query_params.get("error"), **extra)


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user = current_user(request)
    if not user or not user["is_admin"]:
        return need_login(request)
    return await _render_admin(request, user)


@app.post("/admin/users/{user_id}/reset-link", response_class=HTMLResponse)
async def admin_reset_link(request: Request, user_id: int):
    """A reset link for the admin to pass on by hand — the only way back in for
    someone who forgot their password while email is off or failing."""
    user = current_user(request)
    if not user or not user["is_admin"]:
        return need_login(request)
    target = db.one("SELECT * FROM users WHERE id=?", (user_id,))
    if not target:
        return RedirectResponse("/admin?error=No+such+user.", status_code=303)
    return await _render_admin(request, user, reset_link=_reset_url(target),
                               reset_email=target["email"])


@app.post("/admin/users/{user_id}/quota")
async def set_quota(request: Request, user_id: int,
                    quota_services: str = Form(""), quota_ram_mb: str = Form(""),
                    quota_disk_mb: str = Form("")):
    user = current_user(request)
    if not user or not user["is_admin"]:
        return need_login(request)

    def parse(v: str):
        v = v.strip()
        return int(v) if v.isdigit() and int(v) > 0 else None

    db.q("UPDATE users SET quota_services=?, quota_ram_mb=?, quota_disk_mb=? "
         "WHERE id=?", (parse(quota_services), parse(quota_ram_mb),
                        parse(quota_disk_mb), user_id))
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/users/{user_id}/paid")
async def mark_paid(request: Request, user_id: int):
    """Extend a user's paid period by 30 days (manual billing until Stripe
    lands). First payment also credits their referrer's +20% bonus."""
    user = current_user(request)
    if not user or not user["is_admin"]:
        return need_login(request)
    target = db.one("SELECT * FROM users WHERE id=?", (user_id,))
    if target:
        base = max(target["paid_until"] or 0, db.now())
        db.q("UPDATE users SET paid_until=? WHERE id=?",
             (base + 30 * 86400, user_id))
        referrals.record_conversion(user_id)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/limits")
async def set_global_limits(request: Request, max_services: str = Form(""),
                            max_databases: str = Form(""), max_ram_mb: str = Form("")):
    user = current_user(request)
    if not user or not user["is_admin"]:
        return need_login(request)
    current = metrics.global_limits()

    def parse(v: str, fallback: int):
        v = v.strip()
        return int(v) if v.isdigit() and int(v) > 0 else fallback

    metrics.set_global_limits(parse(max_services, current["services"]),
                              parse(max_databases, current["databases"]),
                              parse(max_ram_mb, current["ram_mb"]))
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/universal-quota")
async def set_universal_quota(request: Request, services: str = Form(""),
                              ram_mb: str = Form(""), disk_mb: str = Form(""),
                              databases: str = Form("")):
    user = current_user(request)
    if not user or not user["is_admin"]:
        return need_login(request)
    current = metrics.universal_quota()

    def parse(v: str, fallback: int):
        v = v.strip()
        return int(v) if v.isdigit() and int(v) > 0 else fallback

    metrics.set_universal_quota(parse(services, current["services"]),
                                parse(ram_mb, current["ram_mb"]),
                                parse(disk_mb, current["disk_mb"]),
                                parse(databases, current["databases"]))
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/invites/send")
async def send_invite(request: Request, email: str = Form(...)):
    user = current_user(request)
    if not user or not user["is_admin"]:
        return need_login(request)
    email = email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return RedirectResponse("/admin?error=Invalid+email", status_code=303)
    if db.one("SELECT 1 FROM users WHERE email=?", (email,)):
        return RedirectResponse("/admin?error=That+person+already+has+an+account",
                                status_code=303)
    await asyncio.to_thread(invites.send_invite, email, BASE_URL, user["id"])
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/invites/{invite_id}/approve")
async def approve_invite(request: Request, invite_id: int):
    user = current_user(request)
    if not user or not user["is_admin"]:
        return need_login(request)
    await asyncio.to_thread(invites.approve, invite_id, BASE_URL, user["id"])
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/invites/{invite_id}/revoke")
async def revoke_invite(request: Request, invite_id: int):
    user = current_user(request)
    if not user or not user["is_admin"]:
        return need_login(request)
    invites.revoke(invite_id, user["id"])
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/github/callback")
async def admin_github_callback(request: Request, code: str = ""):
    user = current_user(request)
    if not user or not user["is_admin"]:
        return need_login(request)
    if code:
        await asyncio.to_thread(gh.exchange_manifest_code, code)
    return RedirectResponse("/admin", status_code=303)


# ---------- billing ----------

@app.post("/billing/checkout")
async def billing_checkout(request: Request):
    user = current_user(request)
    if not user:
        return need_login(request)
    if not billing.available():
        return RedirectResponse(
            "/dashboard?error=" + "Payments aren't live yet — email "
            "hello@cicatrixa.com and we'll set you up.".replace(" ", "+"),
            status_code=303)
    projects = db.one("SELECT COUNT(*) c FROM projects WHERE user_id=?",
                      (user["id"],))["c"]
    try:
        url = await asyncio.to_thread(billing.checkout_url, user, projects, BASE_URL)
        return RedirectResponse(url, status_code=303)
    except Exception:
        return RedirectResponse(
            "/dashboard?error=" + "Could not start checkout — try again in a "
            "minute.".replace(" ", "+"), status_code=303)


@app.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request):
    body = await request.body()
    if not billing.verify_signature(body, request.headers.get("Stripe-Signature", "")):
        return JSONResponse({"ok": False, "error": "bad signature"}, status_code=401)
    result = await asyncio.to_thread(billing.handle_event, body)
    return {"ok": True, "result": result}


# ---------- webhooks (watchdog: instant redeploy on push) ----------

@app.post("/api/webhooks/github")
async def github_webhook(request: Request):
    body = await request.body()
    secret = db.setting("gh_app_webhook_secret", "")
    if not gh.verify_webhook(secret, request.headers.get("X-Hub-Signature-256"), body):
        return JSONResponse({"ok": False, "error": "bad signature"}, status_code=401)
    event = request.headers.get("X-GitHub-Event")

    # Hook 7: a pull request we opened closed or merged.
    if event == "pull_request":
        pr = gh.parse_pull_request(body)
        if not pr or pr["action"] not in ("closed", "reopened"):
            return {"ok": True, "ignored": True}
        obs = await asyncio.to_thread(flywheel.find_by_pr, pr["repo_full"], pr["number"])
        if not obs:
            return {"ok": True, "ignored": True}
        merged_at = None
        if pr["state"] == "merged" and pr.get("merged_at"):
            merged_at = _iso_to_epoch(pr["merged_at"])
        state = "open" if pr["action"] == "reopened" else pr["state"]
        await asyncio.to_thread(flywheel.set_pr_closed, obs["id"], state=state,
                                merged_at=merged_at)
        return {"ok": True, "observation": obs["id"], "pr_state": state}

    if event != "push":
        return {"ok": True, "ignored": True}
    parsed = gh.parse_push(body)
    if not parsed:
        return {"ok": True, "ignored": True}

    # Hook 8: somebody pushed to a PR branch of ours. Commits that are not the
    # agent's are the ones that count — a PR a human had to touch is a PR we got
    # wrong, and without this the unattended rate is fiction.
    repo_full, branch, _sha = parsed
    human = await asyncio.to_thread(_count_human_commits, body, repo_full, branch)
    if human:
        return {"ok": True, "human_commits": human}
    repo_full, branch, sha = parsed
    triggered = []
    for s in db.all_(
            "SELECT s.id, s.slug, s.name, s.project_id FROM services s "
            "JOIN projects p ON p.id=s.project_id "
            "WHERE s.repo_full=? AND s.branch=? AND p.autodeploy=1",
            (repo_full, branch)):
        bus.publish(f"project:{s['project_id']}", "log",
                    {"line": f"⟳ webhook: push {sha[:10]} to {repo_full}@{branch} "
                             f"— redeploying {s['name']}"})
        asyncio.create_task(engine.deploy(s["id"], "webhook"))
        triggered.append(s["slug"])
    return {"ok": True, "deployed": triggered}


# ---------- flywheel read API ----------
#
# JSON only, no UI. Two audiences: an owner asking what the agent did on their
# own projects, and an admin asking whether the library is compounding.

@app.get("/api/flywheel/summary")
async def flywheel_summary(request: Request, days: int = 30):
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "authentication required"}, status_code=401)
    days = max(1, min(int(days), 365))
    # A non-admin sees only their own tenancy. The platform-wide numbers are the
    # ones that describe other customers' incident volume.
    scope = None if user["is_admin"] else user["id"]
    data = await asyncio.to_thread(flywheel.summary, days, user_id=scope)
    data["scope"] = "platform" if scope is None else "account"
    return data


@app.get("/api/flywheel/observations")
async def flywheel_observations(request: Request, limit: int = 50):
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "authentication required"}, status_code=401)
    scope = None if user["is_admin"] else user["id"]
    rows = await asyncio.to_thread(flywheel.observations, user_id=scope, limit=limit)
    return {"scope": "platform" if scope is None else "account",
            "count": len(rows), "observations": rows}


@app.get("/api/flywheel/transforms")
async def flywheel_transforms(request: Request, limit: int = 50):
    """The library. Tenant-agnostic and free of customer code by construction,
    so any signed-in user may read it — that is the whole point of it being
    shared."""
    if not current_user(request):
        return JSONResponse({"error": "authentication required"}, status_code=401)
    rows = await asyncio.to_thread(flywheel.transforms, limit)
    return {"count": len(rows), "transforms": rows}


def _iso_to_epoch(value: str) -> float | None:
    try:
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _count_human_commits(body: bytes, repo_full: str, branch: str) -> int:
    """Commits on one of our open PR branches whose author is not the agent."""
    obs = flywheel.find_by_pr_branch(repo_full, branch)
    if not obs:
        return 0
    human = [e for e in gh.push_commit_authors(body)
             if e and e != medic.AGENT_EMAIL.lower()]
    if human:
        flywheel.add_human_commits(obs["id"], len(human))
    return len(human)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="104" fill="#0A0F0C"/>
<path d="M150 150 L256 256" stroke="#FF5257" stroke-width="36" stroke-linecap="round"/>
<path d="M256 256 L362 362" stroke="#40D967" stroke-width="36" stroke-linecap="round"/>
<g stroke="#c9d1d9" stroke-width="17" stroke-linecap="round">
<path d="M168 216 L216 168"/><path d="M211 259 L259 211"/>
<path d="M253 301 L301 253"/><path d="M296 344 L344 296"/></g></svg>"""


@app.get("/favicon.svg", include_in_schema=False)
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(FAVICON_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})
