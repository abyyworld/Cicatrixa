"""Cicatrixa platform control plane — web UI + API + orchestration."""
import asyncio
import hmac
import json
import os
import re

import docker.errors
from fastapi import FastAPI, Form, Request
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               Response, StreamingResponse)
from fastapi.templating import Jinja2Templates

from . import ai, auth, bus, db, dbprovision, engine, gh, mailer, medic, metrics, watchdog

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


# ---------- helpers ----------

def current_user(request: Request):
    uid = auth.session_user_id(request.cookies.get(auth.COOKIE_NAME))
    return db.one("SELECT * FROM users WHERE id=?", (uid,)) if uid else None


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
    return RedirectResponse("/login?next=" + request.url.path, status_code=303)


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
    asyncio.create_task(watchdog.run_forever())


# ---------- public pages ----------

@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    return render(request, "landing.html")


@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    return render(request, "signup.html", error=None)


@app.post("/signup")
async def signup(request: Request, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return render(request, "signup.html", error="That doesn't look like an email.")
    if len(password) < 8:
        return render(request, "signup.html", error="Password must be at least 8 characters.")
    if db.one("SELECT 1 FROM users WHERE email=?", (email,)):
        return render(request, "signup.html", error="An account with that email already exists.")
    is_admin = 1 if (email in ADMIN_EMAILS or not db.one("SELECT 1 FROM users LIMIT 1")) else 0
    uid = db.q("INSERT INTO users(email,pw_hash,is_admin,created_at) VALUES(?,?,?,?)",
               (email, auth.hash_password(password), is_admin, db.now())).lastrowid
    return _start_verification(uid, email)


def _issue_code(user_id: int, email: str) -> bool:
    """Generate + store + email a fresh code. Returns whether it could be sent."""
    if not mailer.available():
        return False
    code = auth.generate_code()
    db.q("UPDATE users SET verify_code=?, verify_expires=? WHERE id=?",
         (code, db.now() + auth.CODE_TTL, user_id))
    asyncio.create_task(asyncio.to_thread(mailer.send_verification_code, email, code))
    return True


def _start_verification(user_id: int, email: str) -> RedirectResponse:
    """Send a code and gate on it — or, if mail isn't configured on this instance,
    verify immediately so local/dev setups without RESEND_API_KEY still work."""
    if _issue_code(user_id, email):
        resp = RedirectResponse("/verify-code", status_code=303)
        resp.set_cookie(auth.PENDING_COOKIE_NAME, auth.make_pending(user_id),
                        max_age=auth.PENDING_TTL, httponly=True, samesite="lax",
                        secure=engine.HTTPS_ENABLED)
        return resp
    db.q("UPDATE users SET email_verified=1 WHERE id=?", (user_id,))
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie(auth.COOKIE_NAME, auth.make_session(user_id), max_age=auth.SESSION_TTL,
                    httponly=True, samesite="lax", secure=engine.HTTPS_ENABLED)
    return resp


def _pending_user(request: Request):
    uid = auth.pending_user_id(request.cookies.get(auth.PENDING_COOKIE_NAME))
    return db.one("SELECT * FROM users WHERE id=?", (uid,)) if uid else None


@app.get("/verify-code", response_class=HTMLResponse)
async def verify_code_page(request: Request):
    user = _pending_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user["email_verified"]:
        return RedirectResponse("/dashboard", status_code=303)
    qp = request.query_params
    return render(request, "verify_code.html", user=None, pending_email=user["email"],
                  error=qp.get("error"), resent=qp.get("resent"))


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
        return render(request, "verify_code.html", user=None, pending_email=user["email"],
                      error="That code is incorrect or has expired.")
    db.q("UPDATE users SET email_verified=1, verify_code=NULL, verify_expires=NULL "
         "WHERE id=?", (user["id"],))
    resp = RedirectResponse("/dashboard?verified=1", status_code=303)
    resp.delete_cookie(auth.PENDING_COOKIE_NAME)
    resp.set_cookie(auth.COOKIE_NAME, auth.make_session(user["id"]), max_age=auth.SESSION_TTL,
                    httponly=True, samesite="lax", secure=engine.HTTPS_ENABLED)
    return resp


@app.post("/verify-code/resend")
async def verify_code_resend(request: Request):
    user = _pending_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    _issue_code(user["id"], user["email"])
    resp = RedirectResponse("/verify-code?resent=1", status_code=303)
    resp.set_cookie(auth.PENDING_COOKIE_NAME, auth.make_pending(user["id"]),
                    max_age=auth.PENDING_TTL, httponly=True, samesite="lax",
                    secure=engine.HTTPS_ENABLED)
    return resp


@app.post("/verify-code/cancel")
async def verify_code_cancel():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.PENDING_COOKIE_NAME)
    return resp


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "login.html", error=None)


@app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    user = db.one("SELECT * FROM users WHERE email=?", (email.strip().lower(),))
    if not user or not auth.verify_password(password, user["pw_hash"]):
        return render(request, "login.html", error="Wrong email or password.")
    if not user["email_verified"]:
        return _start_verification(user["id"], user["email"])
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie(auth.COOKIE_NAME, auth.make_session(user["id"]),
                    max_age=auth.SESSION_TTL, httponly=True, samesite="lax", secure=engine.HTTPS_ENABLED)
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
                  error=qp.get("error"), verified=qp.get("verified"))


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
    service_id, text = await asyncio.to_thread(medic.apply_fix_sync,
                                               project_id, message_id)
    if service_id:
        asyncio.create_task(engine.deploy(service_id, "chat-fix"))
    else:
        medic.add_message(project_id, "agent", f"✖ {text}", kind="status")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/deploy")
async def redeploy(request: Request, project_id: int):
    user, project = own_project(request, project_id)
    if not project:
        return need_login(request)
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
    await asyncio.to_thread(dbprovision.create, project, name)
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
    return StreamingResponse(bus.subscribe(f"project:{project_id}"),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------- GitHub connect ----------

@app.get("/connect/github", response_class=HTMLResponse)
async def connect_github(request: Request):
    user = current_user(request)
    if not user:
        return need_login(request)
    return render(request, "connect.html", user=user, error=None)


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
    state = auth.sign_state(str(user["id"]))
    return RedirectResponse(
        f"https://github.com/apps/{gh.app_slug()}/installations/new?state={state}")


@app.get("/connect/github/setup")
@app.get("/connect/github/callback")
async def connect_setup(request: Request):
    """GitHub sends the user back here after installing the App."""
    params = request.query_params
    installation_id = params.get("installation_id")
    uid = auth.verify_state(params.get("state", ""))
    user = current_user(request)
    if uid is None and user:
        uid = str(user["id"])
    if not installation_id or uid is None:
        return RedirectResponse("/connect/github", status_code=303)
    login = ""
    try:
        token = gh.installation_token(int(installation_id))
        login = ""  # installation tokens can't call /user; store empty
    except Exception:
        pass
    db.q("DELETE FROM github_connections WHERE user_id=? AND kind='app'", (int(uid),))
    db.q("INSERT INTO github_connections(user_id,kind,installation_id,gh_login,created_at)"
         " VALUES(?,?,?,?,?)", (int(uid), "app", int(installation_id), login, db.now()))
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

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user = current_user(request)
    if not user or not user["is_admin"]:
        return need_login(request)
    manifest = gh.build_manifest(BASE_URL)
    await asyncio.to_thread(metrics.ensure_fresh)
    return render(request, "admin.html", user=user,
                  manifest_json=json.dumps(manifest, indent=2), base_url=BASE_URL,
                  host=metrics.host, users_usage=metrics.all_users_usage(),
                  defaults={"services": metrics.DEFAULT_QUOTA_SERVICES,
                            "ram_mb": metrics.DEFAULT_QUOTA_RAM_MB,
                            "disk_mb": metrics.DEFAULT_QUOTA_DISK_MB})


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


@app.get("/admin/github/callback")
async def admin_github_callback(request: Request, code: str = ""):
    user = current_user(request)
    if not user or not user["is_admin"]:
        return need_login(request)
    if code:
        await asyncio.to_thread(gh.exchange_manifest_code, code)
    return RedirectResponse("/admin", status_code=303)


# ---------- webhooks (watchdog: instant redeploy on push) ----------

@app.post("/api/webhooks/github")
async def github_webhook(request: Request):
    body = await request.body()
    secret = db.setting("gh_app_webhook_secret", "")
    if not gh.verify_webhook(secret, request.headers.get("X-Hub-Signature-256"), body):
        return JSONResponse({"ok": False, "error": "bad signature"}, status_code=401)
    if request.headers.get("X-GitHub-Event") != "push":
        return {"ok": True, "ignored": True}
    parsed = gh.parse_push(body)
    if not parsed:
        return {"ok": True, "ignored": True}
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
