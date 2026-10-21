"""AI deploy engine: clone -> analyze -> build -> run -> route -> verify -> heal.

A project is a set of services (e.g. frontend + api), one repo each. Every service
runs as its own container on the internal network with no host ports, gets its own
subdomain, a stable internal hostname (network alias = slug), and env vars pointing
at its sibling services.
"""
import asyncio
import base64
import contextlib
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
from urllib.parse import unquote, urlsplit, urlunsplit

import docker
import httpx

from . import ai, bus, db, gh, mailer

BASE_DOMAIN = os.environ.get("BASE_DOMAIN", "localhost")
BASE_URL = os.environ.get("BASE_URL", f"http://{BASE_DOMAIN}")
HTTPS_ENABLED = BASE_URL.startswith("https://")
NETWORK = os.environ.get("CX_NETWORK", "cxnet")
WORK_ROOT = os.environ.get("WORK_ROOT", "/data/work")
MAX_ATTEMPTS = 3
COMMON_PORTS = [3000, 8000, 8080, 5000, 80, 4000, 8501, 5173, 9000, 3001]
RAM_PER_CONTAINER_MB = int(os.environ.get("RAM_PER_CONTAINER_MB", "768"))
CPU_PER_CONTAINER = float(os.environ.get("CPU_PER_CONTAINER", "0.25"))
# Builds run customer code (npm install, next build, pip compiling wheels) with no
# cap by default, so one heavy build can take the whole host down with it — and
# every customer on it. Each build step's container is capped at this, and at most
# BUILD_CONCURRENCY builds run at once.
BUILD_MEM_MB = int(os.environ.get("BUILD_MEM_MB", "2048"))
BUILD_CONCURRENCY = max(1, int(os.environ.get("BUILD_CONCURRENCY", "2")))
# The :80 middleware for user apps: a fixed name, defined on cx-control in
# docker-compose.yml as a chain over HTTPS_REDIRECT_MW. Labels are frozen when an
# app container is created, so baking the switch's current value in here would
# leave every running app on the old behaviour after the switch flips.
APP_WEB_MW = "cx-app-web"
# The same idea for an app's :443 routers (VPS mode): a fixed name, so what it
# does can change without redeploying anyone. Today it is cx-retry.
APP_TLS_MW = "cx-app-tls"
API_NAME_RE = re.compile(r"(api|backend|server|graphql|rest)", re.I)


def _env_key(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).upper().strip("_") or "SERVICE"


def _is_api_name(name: str) -> bool:
    return bool(API_NAME_RE.search(name or ""))

_locks: dict[int, asyncio.Lock] = {}
_build_sem = asyncio.Semaphore(BUILD_CONCURRENCY)
_docker: docker.DockerClient | None = None


def dock() -> docker.DockerClient:
    global _docker
    if _docker is None:
        _docker = docker.from_env()
    return _docker


def service_url(slug: str) -> str:
    scheme = "https" if HTTPS_ENABLED else "http"
    return f"{scheme}://{slug}.{BASE_DOMAIN}"


def _lock(service_id: int) -> asyncio.Lock:
    return _locks.setdefault(service_id, asyncio.Lock())


async def deploy_project(project_id: int, trigger: str = "manual"):
    services = db.all_("SELECT id FROM services WHERE project_id=?", (project_id,))
    await asyncio.gather(*(deploy(s["id"], trigger) for s in services))


async def deploy(service_id: int, trigger: str = "manual"):
    async with _lock(service_id), _build_sem:
        await asyncio.to_thread(_deploy_sync, service_id, trigger)


def _deploy_sync(service_id: int, trigger: str):
    row = db.one("SELECT * FROM services WHERE id=?", (service_id,))
    if not row:
        return
    project = db.one("SELECT * FROM projects WHERE id=?", (row["project_id"],))
    service = dict(row)
    service["user_id"] = project["user_id"]
    service["project_slug"] = project["slug"]
    siblings = db.one("SELECT COUNT(*) c FROM services WHERE project_id=?",
                      (project["id"],))["c"]
    dep = db.q("INSERT INTO deployments(service_id,trigger,created_at) VALUES(?,?,?)",
               (service_id, trigger, db.now())).lastrowid
    chan = f"project:{project['id']}"
    prefix = f"⟦{service['name']}⟧ " if siblings > 1 else ""
    lines: list[str] = []

    def log(line: str):
        stamped = f"[{time.strftime('%H:%M:%S')}] {prefix}{line}"
        lines.append(stamped)
        db.q("UPDATE deployments SET log=? WHERE id=?", ("\n".join(lines), dep))
        bus.publish(chan, "log", {"line": stamped, "deployment": dep,
                                  "service": service["name"]})

    def set_status(s: str):
        db.q("UPDATE services SET status=? WHERE id=?", (s, service_id))
        refresh_project_status(project["id"])

    try:
        set_status("deploying")
        _run_pipeline(service, project, dep, log)
        db.q("UPDATE deployments SET status='success', finished_at=? WHERE id=?",
             (db.now(), dep))
        set_status("live")
        log(f"✔ deploy complete — {service_url(service['slug'])}")
    except Exception as exc:  # noqa: BLE001 — everything ends up in the deploy log
        log(f"✖ deploy failed: {exc}")
        db.q("UPDATE deployments SET status='failed', finished_at=? WHERE id=?",
             (db.now(), dep))
        # keep serving the previous container if one is still running
        still = _existing_containers(service["slug"])
        was_failed_already = service["status"] == "failed"
        set_status("live" if still else "failed")
        if not still and not was_failed_already:
            _notify_failure(project, service)


def _notify_failure(project, service):
    if not mailer.available():
        return
    owner = db.one("SELECT email FROM users WHERE id=?", (project["user_id"],))
    if not owner:
        return
    try:
        mailer.send_service_failed(owner["email"], project["name"], service["name"],
                                   f"{BASE_URL}/projects/{project['id']}")
    except Exception:
        pass


def refresh_project_status(project_id: int):
    statuses = [s["status"] for s in
                db.all_("SELECT status FROM services WHERE project_id=?", (project_id,))]
    if not statuses:
        agg = "new"
    elif "deploying" in statuses:
        agg = "deploying"
    elif all(s == "live" for s in statuses):
        agg = "live"
    elif all(s == "stopped" for s in statuses):
        agg = "stopped"
    elif any(s == "live" for s in statuses):
        agg = "degraded"
    elif any(s == "failed" for s in statuses):
        agg = "failed"
    else:
        agg = statuses[0]
    db.q("UPDATE projects SET status=? WHERE id=?", (agg, project_id))
    bus.publish(f"project:{project_id}", "status", {"status": agg})


def _run_pipeline(service, project, dep: int, log):
    slug = service["slug"]
    connection = db.one(
        "SELECT * FROM github_connections WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (project["user_id"],))
    if not connection:
        raise RuntimeError("no GitHub connection for this account")

    # ---- quota gate (RAM + storage) ----
    from . import metrics  # late import; metrics depends on this module
    owner = db.one("SELECT * FROM users WHERE id=?", (project["user_id"],))
    quota_err = metrics.check_deploy_quota(owner, service)
    if quota_err:
        raise RuntimeError(quota_err)

    # ---- clone ----
    workdir = os.path.join(WORK_ROOT, slug)
    shutil.rmtree(workdir, ignore_errors=True)
    _drop_plan_dockerfile(slug)                 # it goes with the checkout it was for
    os.makedirs(workdir, exist_ok=True)
    log(f"⇣ cloning {service['repo_full']}@{service['branch']}")
    r = clone_repo(gh.clone_url(connection, service["repo_full"]), service["branch"], workdir)
    if r.returncode != 0:
        raise RuntimeError(f"git clone failed: {r.stderr.strip()[-500:]}")
    sha = subprocess.run(["git", "-C", workdir, "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    db.q("UPDATE deployments SET sha=? WHERE id=?", (sha, dep))
    log(f"  at commit {sha[:10]}")

    # ---- analyze (AI can request deeper file reads) ----
    tree, files = _snapshot(workdir)
    has_df = os.path.exists(os.path.join(workdir, "Dockerfile"))
    siblings = _sibling_info(service)
    read_file = _make_reader(workdir, log)
    log("🧠 analyzing repository" + (" (AI)" if ai.available() else " (heuristics)"))
    plan = ai.build_plan(tree, files, has_df, siblings=siblings, read_file=read_file) \
        or ai.heuristic_plan(workdir)
    if has_df and not plan.get("dockerfile"):
        plan["dockerfile"] = None
        log("  using repo's own Dockerfile")
    if plan.get("notes"):
        log(f"  plan: {plan['notes']} (port {plan.get('port')})")
    # bridge default when heuristics only: name says this is the project's API
    if plan.get("api_prefixes") is None and siblings and _is_api_name(service["name"]) \
            and not ai.available():
        plan["api_prefixes"] = ["/api"]
    plan["api_prefixes"] = [p for p in (plan.get("api_prefixes") or [])
                            if isinstance(p, str) and p.startswith("/")]
    if plan["api_prefixes"]:
        log(f"  api bridge: {', '.join(plan['api_prefixes'])} will be served on sibling domains")
    buildargs = _build_args(service, siblings, plan)

    # ---- build/run with self-healing retries ----
    image = f"cx-{slug}:{sha[:10]}"
    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        log(f"🔨 build attempt {attempt}/{MAX_ATTEMPTS}")
        dockerfile_rel, dockerfile_text = "Dockerfile", None
        if plan.get("dockerfile"):
            dockerfile_rel, dockerfile_text = PLAN_DOCKERFILE, plan["dockerfile"]
            _save_plan_dockerfile(slug, dockerfile_text)
        try:
            build_log = _build(workdir, dockerfile_rel, image, log, buildargs, dockerfile_text)
        except RuntimeError as exc:
            last_error = str(exc)
            log(f"  build failed: {last_error[-400:]}")
            plan = _heal(plan, last_error, tree, files, log, siblings, read_file) or plan
            buildargs = _build_args(service, siblings, plan)
            continue

        port, container, run_log = _start_and_probe(service, image, plan, log)
        if port:
            _finish(service, container, image, port, plan, sha, log, tree, files)
            _integration_audit(service, workdir, plan, port, container, sha,
                               siblings, read_file, tree, files, log, buildargs)
            return
        last_error = f"container did not serve HTTP.\nBUILD LOG:\n{build_log[-3000:]}" \
                     f"\nRUNTIME LOG:\n{run_log[-6000:]}"
        log("  app did not respond on any candidate port")
        plan = _heal(plan, last_error, tree, files, log, siblings, read_file) or plan
        buildargs = _build_args(service, siblings, plan)

    raise RuntimeError(f"gave up after {MAX_ATTEMPTS} attempts. Last error: "
                       f"{last_error[-800:]}")


def _heal(plan, error_log, tree, files, log, siblings=None, read_file=None):
    if not ai.available():
        return None
    log("🩹 asking AI to diagnose and fix the build (it may read more source files)")
    current_df = plan.get("dockerfile") or "(repo's own Dockerfile)"
    fix = ai.fix_plan(current_df, error_log, tree, files,
                      siblings=siblings, read_file=read_file)
    if not fix:
        log("  no fix produced")
        return None
    if fix.get("diagnosis"):
        log(f"  diagnosis: {fix['diagnosis']}")
    merged = dict(plan)
    if fix.get("dockerfile"):
        merged["dockerfile"] = fix["dockerfile"]
    if isinstance(fix.get("port"), int):
        merged["port"] = fix["port"]
    if fix.get("health_path"):
        merged["health_path"] = fix["health_path"]
    if isinstance(fix.get("build_args"), dict):
        merged["build_args"] = {**(merged.get("build_args") or {}), **fix["build_args"]}
    return merged


def _sibling_info(service) -> list[dict]:
    return [{"name": s["name"], "slug": s["slug"], "port": s["port"],
             "public_url": service_url(s["slug"]), "env_key": _env_key(s["name"])}
            for s in db.all_("SELECT name, slug, port FROM services WHERE project_id=? "
                             "AND id!=?", (service["project_id"], service["id"]))]


def _make_reader(workdir: str, log):
    """Sandboxed file reader the AI uses to dig deeper into the repo."""
    def read_file(relpath: str):
        try:
            text = read_checkout_file(workdir, relpath, 8000)
        except OSError:
            return None
        if text is not None:
            log(f"  🔎 AI reading {relpath}")
        return text
    return read_file


# ---------- checkouts: what customer code and the AI may see of them ----------
#
# A checkout is not private. The AI reads files in it on the say-so of whatever the
# repo told it, it is the build context (the static plan's `COPY . /usr/share/nginx/html`
# serves every file in it to anyone, and a repo's own Dockerfile can print any), and
# the medic's test runs get a copy. So nothing in it may hold a credential, and git's
# own directory — the remote URL, and config git acts on (hooks, core.fsmonitor) — is
# never read, written, built or copied on anyone's behalf.

def split_credentials(url: str) -> tuple[str, dict]:
    """(the URL without its user:token, the environment that hands git the token).

    git stores the URL a clone came from in .git/config, and an installation token
    there reaches every repo of the installation with write access (a PAT never even
    expires). So the token travels as an HTTP header set through git's environment
    config (GIT_CONFIG_COUNT, git 2.31+): git sends it for that one command and writes
    it nowhere. GitHub takes the same Basic credential as the user:token URL."""
    parts = urlsplit(url)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if not parts.username:
        return url, env
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    clean = urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    basic = base64.b64encode(
        f"{unquote(parts.username)}:{unquote(parts.password or '')}".encode()).decode()
    n = int(env.get("GIT_CONFIG_COUNT") or 0)       # added to any already set, not over them
    env.update({"GIT_CONFIG_COUNT": str(n + 1),
                f"GIT_CONFIG_KEY_{n}": f"http.{parts.scheme}://{host}/.extraHeader",
                f"GIT_CONFIG_VALUE_{n}": f"Authorization: Basic {basic}"})
    return clean, env


def clone_repo(url: str, branch: str, workdir: str, depth: int | None = 1):
    """git clone, leaving no credential in the checkout (split_credentials)."""
    clean, env = split_credentials(url)
    shallow = ["--depth", str(depth)] if depth else []
    return subprocess.run(["git", "clone", *shallow, "--branch", branch, clean, workdir],
                          capture_output=True, text=True, timeout=300, env=env)


def _in_git_dir(relpath: str) -> bool:
    return any(part.lower() == ".git" for part in re.split(r"[\\/]", relpath))


def inside_checkout(root: str, relpath: str) -> str | None:
    """The real path of `relpath` in the checkout at `root` — or None when it leaves
    the checkout, or lands in git's own files (checked after symlinks resolve, so a
    link to .git is refused too). Every path the AI names goes through here."""
    base = os.path.realpath(root)
    full = os.path.realpath(os.path.join(base, (relpath or "").lstrip("/")))
    if not full.startswith(base + os.sep) or _in_git_dir(os.path.relpath(full, base)):
        return None
    return full


def read_checkout_file(root: str, relpath: str, limit: int = -1,
                       errors: str = "replace") -> str | None:
    """The text of `relpath` in the checkout at `root`, read as open(path) reads it —
    or None unless it is a regular file inside the checkout (inside_checkout).

    A repo chooses what its names point at: README.md -> /proc/self/environ (this
    process's environment: every API key cx-control holds), requirements.txt -> another
    tenant's checkout beside this one, Procfile -> .git/config. Each is one commit away,
    and what is read here goes into the AI's prompt, and through the medic into replies
    to the repo's owner. So every read of a name the repo chose comes through here. The
    real path has no link left in it, so O_NOFOLLOW refuses only one made since; and
    O_NONBLOCK, so a pipe is refused by the S_ISREG check instead of hanging the open."""
    full = inside_checkout(root, relpath)
    if full is None:
        return None
    try:
        fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
    except OSError:
        regular = False
    if not regular:
        os.close(fd)
        return None
    with open(fd, errors=errors) as f:                  # closes fd
        return f.read(limit)


# The Dockerfile the platform wrote for a repo that has none (the plan's). It is kept
# beside the checkouts, never in one: a checkout's names are the repo's, and a repo that
# commits `.cx.Dockerfile` as a link to /data/cicatrixa.db had the plan written through
# it, over the platform's database. The build gets it as an extra file of its context,
# under this name (_build_context), and the medic reads it from here. A slug has no dot
# (main.make_slug, import_online._SLUG), so `<slug>.cx.Dockerfile` is never a checkout.
PLAN_DOCKERFILE = ".cx.Dockerfile"


def plan_dockerfile_path(slug: str) -> str:
    return os.path.join(WORK_ROOT, slug + PLAN_DOCKERFILE)


def _save_plan_dockerfile(slug: str, text: str):
    # Whole or not at all: the medic may read it while a deploy writes it.
    os.makedirs(WORK_ROOT, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=WORK_ROOT, prefix=f".{slug}.", suffix=".tmp")
    try:
        with open(fd, "w") as f:
            f.write(text)
        os.replace(tmp, plan_dockerfile_path(slug))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_plan_dockerfile(slug: str) -> str | None:
    try:
        with open(plan_dockerfile_path(slug)) as f:
            return f.read()
    except FileNotFoundError:
        return None


def _drop_plan_dockerfile(slug: str):
    with contextlib.suppress(FileNotFoundError):
        os.unlink(plan_dockerfile_path(slug))


def _build_context(workdir: str, dockerfile: str, dockerfile_text: str | None = None):
    """The tar docker-py 7.1's api.build(path=...) would send, minus .git.

    Its own rules, mirrored (api/build.py → utils.tar): the repo's .dockerignore
    patterns, with the Dockerfile always sent. .git goes whatever the .dockerignore
    says: nothing in it is needed to build, and a `COPY .` would publish it.

    With `dockerfile_text` (the plan's, which is not in the checkout), that text goes in
    under the name `dockerfile`, and whatever the repo committed under that name stays
    out: the build reads the platform's file, not one the repo planted."""
    # Here, not at the top: workspace/test/plan.test.js runs this module's planning
    # half under a python whose `docker` may be a stub.
    from docker.utils.build import create_archive, exclude_paths
    root = os.path.abspath(workdir)
    # Through read_checkout_file, as every name the repo chose: a .dockerignore that
    # links elsewhere would make that file's lines this build's patterns.
    text = read_checkout_file(root, ".dockerignore") or ""
    patterns = [p for p in (line.strip() for line in text.splitlines())
                if p and not p.startswith("#")]
    # ".git" last, so no "!" exception before it brings any of it back; the filter
    # below is the guarantee, the pattern only spares walking it.
    files = exclude_paths(root, patterns + [".git"], dockerfile=dockerfile)
    extra = []
    if dockerfile_text is not None:
        # A directory of that name too, and all under it: one path, one entry.
        files = {f for f in files if f != dockerfile and not f.startswith(dockerfile + os.sep)}
        extra = [(dockerfile, dockerfile_text)]
    return create_archive(root, files=sorted(f for f in files if not _in_git_dir(f)),
                          extra_files=extra)


def _build_args(service, siblings: list[dict], plan) -> dict:
    """Build-time wiring: sibling URLs + framework-standard API vars, plus AI extras."""
    args = {}
    for s in siblings:
        args[f"{s['env_key']}_URL"] = s["public_url"]
    apiish = [s for s in siblings if _is_api_name(s["name"])]
    if len(apiish) == 1 and not _is_api_name(service["name"]):
        url = apiish[0]["public_url"]
        for key in ("API_URL", "BACKEND_URL", "VITE_API_URL", "NEXT_PUBLIC_API_URL",
                    "REACT_APP_API_URL", "PUBLIC_API_URL"):
            args.setdefault(key, url)
    for k, v in (plan.get("build_args") or {}).items():
        if isinstance(k, str) and isinstance(v, str):
            args[k] = v
    return args


def _build(workdir: str, dockerfile: str, tag: str, log, buildargs: dict | None = None,
           dockerfile_text: str | None = None) -> str:
    """Builds `workdir` as `tag`. `dockerfile_text`: the plan's Dockerfile, sent as
    `dockerfile` in the context instead of anything of that name in the checkout."""
    api = dock().api
    out = []
    # memswap == memory means no swap on top: an over-limit step fails the build,
    # which the heal loop reports, rather than thrashing the host for everyone.
    limit = BUILD_MEM_MB * 1024 * 1024
    with _build_context(workdir, dockerfile, dockerfile_text) as context:
        for chunk in api.build(fileobj=context, custom_context=True, dockerfile=dockerfile,
                               tag=tag, rm=True, decode=True, buildargs=buildargs or {},
                               container_limits={"memory": limit, "memswap": limit}):
            if "stream" in chunk:
                text = chunk["stream"].rstrip()
                if text:
                    out.append(text)
                    if text.startswith(("Step", "#", " --->")) or "Successfully" in text:
                        log(f"    {text[:160]}")
            if "errorDetail" in chunk:
                out.append(chunk["errorDetail"].get("message", ""))
                raise RuntimeError("\n".join(out[-30:]))
    return "\n".join(out)


def test_runner(image: str, workdir: str, timeout: int = 600):
    """A callable that runs one shell command inside `image` and returns
    (exit_code, output) — what verify.collect() needs to execute a customer's
    suite.

    The clone is COPIED in, at the same path, before the container starts. It
    lives in the platform's /data volume, and that volume must never be mounted
    here: this is the customer's own code, and /data also holds cicatrixa.db —
    the key that signs every session, everyone's password hashes, the GitHub
    App's private key — and every other tenant's checkout. (A bind mount of
    the control plane's path would not work either: the host daemon resolves it.)

    entrypoint="" because the image is the customer's app — its ENTRYPOINT would
    start their server instead of pytest. The same RAM/CPU caps as a user
    container apply, so a runaway suite cannot take the host down. The copy has
    no .git: a suite needs none of it, and it is not the customer's code.
    """
    arcroot = workdir.lstrip("/")

    def no_git(info: tarfile.TarInfo):
        return None if _in_git_dir(info.name[len(arcroot):]) else info

    def run(command: str) -> tuple[int, str]:
        container = dock().containers.create(
            image, entrypoint="", command=["sh", "-lc", command],
            working_dir=workdir, labels={"cx.test": "1"},
            mem_limit=f"{RAM_PER_CONTAINER_MB}m",
            nano_cpus=int(CPU_PER_CONTAINER * 1e9),
        )
        try:
            with tempfile.TemporaryFile() as archive:
                with tarfile.open(fileobj=archive, mode="w") as tar:
                    tar.add(workdir, arcname=arcroot, filter=no_git)
                archive.seek(0)
                container.put_archive("/", archive)
            container.start()
            result = container.wait(timeout=timeout)
            return (result.get("StatusCode", 1),
                    container.logs().decode("utf-8", errors="replace"))
        finally:
            try:
                container.remove(force=True)
            except Exception:
                pass
    return run


def _labels(service, port: int, plan: dict | None = None) -> dict:
    slug = service["slug"]
    labels = {
        "cx.project": str(service["project_id"]),
        "cx.service": slug,
        "cx.user": str(service.get("user_id", "")) if isinstance(service, dict)
                   else str(service["user_id"]),
        "traefik.enable": "true",
        "traefik.docker.network": NETWORK,
        f"traefik.http.routers.cx-{slug}.rule": f"Host(`{slug}.{BASE_DOMAIN}`)",
        f"traefik.http.routers.cx-{slug}.entrypoints": "web",
        f"traefik.http.services.cx-{slug}.loadbalancer.server.port": str(port),
    }
    if HTTPS_ENABLED:
        labels[f"traefik.http.routers.cx-{slug}.middlewares"] = APP_WEB_MW
        labels[f"traefik.http.routers.cx-{slug}-secure.rule"] = f"Host(`{slug}.{BASE_DOMAIN}`)"
        labels[f"traefik.http.routers.cx-{slug}-secure.entrypoints"] = "websecure"
        labels[f"traefik.http.routers.cx-{slug}-secure.service"] = f"cx-{slug}"
        labels[f"traefik.http.routers.cx-{slug}-secure.tls.certresolver"] = "le"
        labels[f"traefik.http.routers.cx-{slug}-secure.middlewares"] = APP_TLS_MW
    # api bridge: serve this service's API prefixes on the sibling frontends' domains,
    # so frontends call a relative /api/... — same origin, no CORS, no baked URLs
    prefixes = (plan or {}).get("api_prefixes") or []
    if prefixes:
        hosts = [s["slug"] for s in _sibling_info(service) if not _is_api_name(s["name"])]
        if hosts:
            host_rule = " || ".join(f"Host(`{h}.{BASE_DOMAIN}`)" for h in hosts)
            path_rule = " || ".join(f"PathPrefix(`{p}`)" for p in prefixes)
            rule = f"({host_rule}) && ({path_rule})"
            router = f"cx-{slug}-bridge"
            strip_mw = None
            if plan.get("strip_prefix"):
                strip_mw = f"cx-{slug}-strip"
                labels[f"traefik.http.middlewares.{strip_mw}.stripprefix.prefixes"] = \
                    ",".join(prefixes)
            labels[f"traefik.http.routers.{router}.rule"] = rule
            labels[f"traefik.http.routers.{router}.entrypoints"] = "web"
            labels[f"traefik.http.routers.{router}.service"] = f"cx-{slug}"
            # The :80 bridge is the whole bridge in tunnel mode, so it needs what the
            # app's own :80 router gets (APP_WEB_MW) — first, before any prefix strip.
            web_mws = ([APP_WEB_MW] if HTTPS_ENABLED else []) + ([strip_mw] if strip_mw else [])
            if web_mws:
                labels[f"traefik.http.routers.{router}.middlewares"] = ",".join(web_mws)
            if HTTPS_ENABLED:
                # frontends are served over https, so their /api fetches land on :443 too
                sec = f"{router}-secure"
                labels[f"traefik.http.routers.{sec}.rule"] = rule
                labels[f"traefik.http.routers.{sec}.entrypoints"] = "websecure"
                labels[f"traefik.http.routers.{sec}.service"] = f"cx-{slug}"
                labels[f"traefik.http.routers.{sec}.tls.certresolver"] = "le"
                labels[f"traefik.http.routers.{sec}.middlewares"] = \
                    ",".join([APP_TLS_MW] + ([strip_mw] if strip_mw else []))
    return labels


def _sibling_env(service) -> dict:
    """Wire sibling services in: FRONTEND_URL / API_URL style vars, plus stable
    internal hostnames (network alias = slug, same port as inside the container)."""
    env = {}
    apiish = []
    for s in db.all_("SELECT name, slug, port FROM services WHERE project_id=? "
                     "AND id!=?", (service["project_id"], service["id"])):
        key = _env_key(s["name"])
        env[f"{key}_URL"] = service_url(s["slug"])
        env[f"{key}_INTERNAL_HOST"] = s["slug"]
        if s["port"]:
            env[f"{key}_INTERNAL_URL"] = f"http://{s['slug']}:{s['port']}"
        if _is_api_name(s["name"]):
            apiish.append(s)
    if len(apiish) == 1 and not _is_api_name(service.get("name", "")):
        env.setdefault("API_URL", service_url(apiish[0]["slug"]))
        if apiish[0]["port"]:
            env.setdefault("API_INTERNAL_URL",
                           f"http://{apiish[0]['slug']}:{apiish[0]['port']}")
    return env


def _run_container(service, image: str, port: int, name: str, plan: dict | None = None,
                   routed: bool = True):
    from . import dbprovision  # late import: dbprovision imports this module
    env = {"PORT": str(port), "HOST": "0.0.0.0",
           "PUBLIC_URL": service_url(service["slug"]), **_sibling_env(service),
           **dbprovision.sibling_env(service["project_id"])}
    labels = _labels(service, port, plan)
    if not routed:
        # A candidate under test takes no traffic and answers to no sibling:
        # Traefik ignores it, and only its own name reaches it.
        labels = {k: v for k, v in labels.items() if k.startswith("cx.")}
        labels["traefik.enable"] = "false"
    client = dock()
    container = client.containers.create(
        image, name=name, labels=labels,
        mem_limit=f"{RAM_PER_CONTAINER_MB}m", nano_cpus=int(CPU_PER_CONTAINER * 1e9),
        restart_policy={"Name": "unless-stopped"}, environment=env,
    )
    net = client.networks.get(NETWORK)
    try:
        client.networks.get("bridge").disconnect(container)
    except Exception:
        pass
    net.connect(container, aliases=[service["slug"]] if routed else [])
    container.start()
    return container


def _start_and_probe(service, image: str, plan: dict, log):
    """Start a candidate with no traffic, find the port it really listens on, and
    only then start the copy that takes traffic.

    The candidate used to carry the live router's labels from its first second,
    so Traefik put it in the load balancer beside the old container at once:
    a push that crashed at boot served errors to about half of the app's
    visitors for each probe attempt, minutes in all. The routed copy still joins
    while it starts, but cx-retry (docker-compose.yml) sends a request it cannot
    connect to on to the old container."""
    slug = service["slug"]
    planned = int(plan.get("port") or 8000)
    name = f"cx-{slug}-{int(time.time())}"
    log(f"🚀 starting candidate {name} (expecting port {planned}) — no traffic yet")
    container = _run_container(service, image, planned, name, plan, routed=False)

    health = plan.get("health_path") or "/"
    candidates = [planned] + [p for p in _exposed_ports(image) if p != planned] \
                 + [p for p in COMMON_PORTS if p != planned]
    found = _probe(name, candidates[:12], health, log, timeout=75)

    container.reload()
    run_log = container.logs(tail=300).decode(errors="replace")
    if found is None:
        if container.status != "running":
            log(f"  container exited (status={container.status})")
        _safe_rm(container)
        return None, None, run_log
    if found != planned:
        log(f"  app actually listens on {found}, not {planned}")
    log(f"  candidate works — starting the copy that takes traffic, on port {found}")
    _safe_rm(container)
    name = f"cx-{slug}-{int(time.time())}"
    container = _run_container(service, image, found, name, plan)
    if _probe(name, [found], health, log, timeout=45) is None:
        run_log = container.logs(tail=300).decode(errors="replace")
        _safe_rm(container)
        return None, None, run_log
    return found, name, run_log


def _probe(host: str, ports: list[int], health: str, log, timeout: int):
    deadline = time.time() + timeout
    logged = set()
    while time.time() < deadline:
        for port in ports:
            try:
                resp = httpx.get(f"http://{host}:{port}{health}", timeout=3,
                                 follow_redirects=True)
                if resp.status_code < 500:
                    log(f"  ✓ HTTP {resp.status_code} on port {port}{health}")
                    return port
                if port not in logged:
                    logged.add(port)
                    log(f"  port {port} answered HTTP {resp.status_code}")
            except Exception:
                pass
        time.sleep(2)
    return None


def _exposed_ports(image: str) -> list[int]:
    try:
        cfg = dock().images.get(image).attrs.get("Config", {})
        return [int(p.split("/")[0]) for p in (cfg.get("ExposedPorts") or {})]
    except Exception:
        return []


def _finish(service, container_name: str, image: str, port: int, plan, sha, log,
            tree: str = "", files: dict | None = None):
    slug, sid = service["slug"], service["id"]
    # blue/green: drop older containers only after the new one is healthy
    for old in _existing_containers(slug):
        if old.name != container_name:
            log(f"♻ retiring previous container {old.name}")
            _safe_rm(old)
    health = plan.get("health_path") or "/"
    db.q("UPDATE services SET container=?, image=?, port=?, health_path=?, last_sha=?, "
         "api_prefix=? WHERE id=?",
         (container_name, image, port, health, sha,
          ",".join(plan.get("api_prefixes") or []) or None, sid))
    url = service_url(slug)
    # smoke test through the same path a user would hit
    try:
        resp = httpx.get(f"http://{container_name}:{port}{health}",
                         timeout=10, follow_redirects=True)
        run_log = dock().containers.get(container_name).logs(tail=100) \
                                   .decode(errors="replace")
        log(f"🧪 smoke test: HTTP {resp.status_code}, {len(resp.content)} bytes")
        if resp.status_code >= 400 and files:
            # dig deeper: read the code, find real routes, verify one actually answers
            log("  🔎 probe path answered ≥400 — AI is reading the code for real routes")
            for path in ai.probe_paths(tree, files, run_log, resp.status_code):
                try:
                    deep = httpx.get(f"http://{container_name}:{port}{path}", timeout=6,
                                     follow_redirects=True)
                    log(f"    {path} → HTTP {deep.status_code}")
                    if deep.status_code < 400:
                        resp = deep
                        db.q("UPDATE services SET health_path=? WHERE id=?", (path, sid))
                        log(f"  ✓ verified working endpoint {path} — recorded as health path")
                        break
                except Exception:
                    log(f"    {path} → unreachable")
        verdict = ai.smoke_verdict(url, resp.status_code, resp.text[:1500], run_log)
        if verdict:
            log(f"🧠 AI verdict: {verdict}")
    except Exception as exc:
        log(f"🧪 smoke test error: {exc}")
    _prune_images(slug, keep=image)


def _existing_containers(slug: str):
    try:
        return dock().containers.list(all=True, filters={"label": f"cx.service={slug}"})
    except Exception:
        return []


def _safe_rm(container):
    try:
        container.stop(timeout=8)
    except Exception:
        pass
    try:
        container.remove(force=True)
    except Exception:
        pass


def _prune_images(slug: str, keep: str):
    try:
        for img in dock().images.list(name=f"cx-{slug}"):
            tags = img.tags or []
            if keep not in tags and any(t.startswith(f"cx-{slug}:") for t in tags):
                dock().images.remove(img.id, force=True)
    except Exception:
        pass


# ---------- integration audit: does the frontend actually talk to its sibling API? ----------

URL_WHITELIST = ("w3.org", "youtube", "youtu.be", "vimeo", "plyr", "googleapis",
                 "gstatic", "unpkg", "jsdelivr", "cdn.", "reactrouter", "react.dev",
                 "noembed", "schema.org", "github.com", "npmjs", "mozilla.org",
                 "fb.me", "ytimg.com", "aniview")


def _foreign_urls(text: str) -> set[str]:
    found: set[str] = set()
    for url in re.findall(r'https?://[A-Za-z0-9.\-]+(?::\d+)?', text):
        host = url.split("//", 1)[1]
        if BASE_DOMAIN in host or any(w in host for w in URL_WHITELIST):
            continue
        if host in ("localhost", "127.0.0.1"):
            continue  # bare localhost strings are dev-mode noise; ports are real
        found.add(url)
    return found


def _bundle_urls(container_name: str, port: int, log) -> list[str]:
    """Fetch the frontend's served HTML (and any external JS it loads) and list
    foreign API URLs — whether they live in an external bundle or inline <script>."""
    try:
        index = httpx.get(f"http://{container_name}:{port}/", timeout=8).text
    except Exception:
        return []
    found = _foreign_urls(index)  # catches inline <script> blocks, plain static sites
    scripts = re.findall(r'src="(/[^"]+\.m?js[^"]*)"', index)[:3]
    for src in scripts:
        try:
            js = httpx.get(f"http://{container_name}:{port}{src}", timeout=10).text
        except Exception:
            continue
        found |= _foreign_urls(js)
    return sorted(found)[:5]


def _grep_urls(workdir: str, urls: list[str]) -> dict[str, list[str]]:
    """Locate which repo files (source AND committed build output) contain each URL."""
    hits: dict[str, list[str]] = {u: [] for u in urls}
    root = os.path.realpath(workdir)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules")]
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, root)
            try:
                # Files that are in the checkout itself, never a link: the repo points
                # those anywhere (read_checkout_file), and a file a link names inside
                # the checkout is found here under its own name.
                st = os.lstat(full)
                if not stat.S_ISREG(st.st_mode) or st.st_size > 3_000_000:
                    continue
                content = read_checkout_file(root, rel)
            except OSError:
                continue
            if content is None:
                continue
            for u in urls:
                if u in content and len(hits[u]) < 10:
                    hits[u].append(rel)
    return {u: paths for u, paths in hits.items() if paths}


def _integration_audit(service, workdir, plan, port, container_name, sha,
                       siblings, read_file, tree, files, log, buildargs):
    """Frontend + api sibling: verify the built bundle targets the sibling, not a
    foreign host; if not, let the AI patch the source and rebuild once."""
    apiish = [s for s in siblings if _is_api_name(s["name"])]
    if not apiish or _is_api_name(service["name"]) or not ai.available():
        return
    suspicious = _bundle_urls(container_name, port, log)
    if not suspicious:
        return
    log(f"🔬 integration audit: bundle calls foreign API host(s): {', '.join(suspicious)}")
    log("  🔎 AI is reading the frontend source to rewire it to the sibling API")
    api_row = db.one("SELECT api_prefix FROM services WHERE slug=?", (apiish[0]["slug"],))
    prefix = (api_row["api_prefix"] if api_row and api_row["api_prefix"] else "/api")
    url_locations = _grep_urls(workdir, suspicious)
    for u, paths in url_locations.items():
        log(f"  found {u} in: {', '.join(paths[:4])}")
    fix = ai.integration_fix(suspicious, prefix.split(",")[0], True, siblings,
                             tree, files, read_file, url_locations=url_locations)
    if not fix or fix.get("ok") or not fix.get("patches"):
        log(f"  audit verdict: {(fix or {}).get('diagnosis') or 'no safe patch produced'}"
            " — leaving the build as is")
        return
    log(f"  diagnosis: {fix.get('diagnosis', '')}")
    applied = 0
    for patch in fix["patches"][:6]:
        rel, find, repl = patch.get("file", ""), patch.get("find"), patch.get("replace")
        full = inside_checkout(workdir, rel)
        if full is None or not find:
            continue
        try:
            src = open(full, errors="replace").read()
        except OSError:
            log(f"  ⚠ patch target not found: {rel}")
            continue
        if find not in src:
            log(f"  ⚠ pattern not found in {rel}")
            continue
        with open(full, "w") as f:
            f.write(src.replace(find, repl or ""))
        log(f"  🩹 patched {rel}")
        applied += 1
    if not applied:
        return
    merged_args = {**buildargs, **(fix.get("build_args") or {})}
    image2 = f"cx-{service['slug']}:{sha[:10]}-i"
    log("🔨 rebuilding with integration patches")
    try:
        _build(workdir, PLAN_DOCKERFILE if plan.get("dockerfile") else "Dockerfile",
               image2, log, merged_args, plan.get("dockerfile"))
    except RuntimeError as exc:
        log(f"  ✖ integration rebuild failed, keeping original build: {str(exc)[-300:]}")
        return
    port2, container2, _run_log = _start_and_probe(service, image2, plan, log)
    if not port2:
        log("  ✖ patched build did not serve — keeping original build")
        return
    _finish(service, container2, image2, port2, plan, sha, log, tree, files)
    remaining = _bundle_urls(container2, port2, log)
    if remaining:
        log(f"  audit after patch: still sees {', '.join(remaining)}")
    else:
        log("  ✓ integration audit clean — frontend now targets its sibling API")


# ---------- lifecycle ----------

def stop_service(service):
    for c in _existing_containers(service["slug"]):
        _safe_rm(c)
    db.q("UPDATE services SET status='stopped', container=NULL WHERE id=?",
         (service["id"],))
    refresh_project_status(service["project_id"])


def delete_service(service, revoke_github: bool = True):
    stop_service(service)
    try:
        for img in dock().images.list(name=f"cx-{service['slug']}"):
            dock().images.remove(img.id, force=True)
    except Exception:
        pass
    shutil.rmtree(os.path.join(WORK_ROOT, service["slug"]), ignore_errors=True)
    _drop_plan_dockerfile(service["slug"])
    db.q("DELETE FROM deployments WHERE service_id=?", (service["id"],))
    db.q("DELETE FROM services WHERE id=?", (service["id"],))
    if revoke_github and service["repo_full"]:
        gh.revoke_repo(service["repo_full"])
    refresh_project_status(service["project_id"])


def stop_project(project):
    for s in db.all_("SELECT * FROM services WHERE project_id=?", (project["id"],)):
        stop_service(s)


def delete_project(project, revoke_github: bool = True):
    from . import dbprovision
    for s in db.all_("SELECT * FROM services WHERE project_id=?", (project["id"],)):
        delete_service(s, revoke_github=revoke_github)
    for d in db.all_("SELECT id FROM databases WHERE project_id=?", (project["id"],)):
        dbprovision.delete(d["id"])
    db.q("DELETE FROM chat_messages WHERE project_id=?", (project["id"],))
    db.q("DELETE FROM projects WHERE id=?", (project["id"],))


# ---------- repo snapshot for the AI ----------

KEY_FILES = ["package.json", "requirements.txt", "pyproject.toml", "go.mod", "Gemfile",
             "Dockerfile", "docker-compose.yml", "Procfile", "next.config.js",
             "next.config.mjs", "vite.config.js", "vite.config.ts", "main.py", "app.py",
             "server.py", "index.js", "server.js", "app.js", "README.md", "Makefile",
             "nuxt.config.ts", "angular.json", "composer.json", "index.html"]


def _snapshot(root: str) -> tuple[str, dict[str, str]]:
    tree_lines = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "node_modules", "__pycache__", ".next",
                                    "venv", ".venv", "target")][:20]
        if depth > 2:
            dirnames[:] = []
            continue
        prefix = "" if rel == "." else rel + "/"
        for f in sorted(filenames)[:40]:
            tree_lines.append(prefix + f)
        if len(tree_lines) > 400:
            break
    files = {}
    for name in KEY_FILES:
        # A key file the repo made a link out of the checkout is left out, not
        # followed (read_checkout_file): these go into the AI's prompt verbatim.
        try:
            text = read_checkout_file(root, name, 6000)
        except Exception:
            continue
        if text is not None:
            files[name] = text
    return "\n".join(tree_lines), files
