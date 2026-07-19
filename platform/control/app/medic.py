"""Chat medic: the user asks for an error check or a fix in chat; the AI investigates
the live containers and the code, proposes exact patches, and — once the user approves —
commits them to the user's GitHub repository and redeploys."""
import json
import os
import re
import shutil
import subprocess
import time

from . import ai, bus, db, engine, gh

PUSH_ROOT = os.environ.get("PUSH_ROOT", "/data/push")
AGENT_NAME = "Cicatrixa Agent"
AGENT_EMAIL = "agent@cicatrixa.dev"


def add_message(project_id: int, role: str, content: str, kind: str = "text",
                data: dict | None = None) -> int:
    mid = db.q("INSERT INTO chat_messages(project_id,role,kind,content,data,created_at) "
               "VALUES(?,?,?,?,?,?)",
               (project_id, role, kind, content,
                json.dumps(data) if data else None, db.now())).lastrowid
    bus.publish(f"project:{project_id}", "chat",
                {"id": mid, "role": role, "kind": kind, "content": content,
                 "data": data or {}})
    return mid


def history(project_id: int) -> list[dict]:
    rows = db.all_("SELECT * FROM chat_messages WHERE project_id=? ORDER BY id",
                   (project_id,))
    out = []
    for r in rows:
        m = dict(r)
        m["data"] = json.loads(r["data"]) if r["data"] else {}
        out.append(m)
    return out


# ---------- investigation ----------

def _ensure_workdir(service, connection) -> str | None:
    workdir = os.path.join(engine.WORK_ROOT, service["slug"])
    if os.path.isdir(os.path.join(workdir, ".git")) or os.path.isdir(workdir):
        return workdir
    try:
        url = gh.clone_url(connection, service["repo_full"])
        r = subprocess.run(["git", "clone", "--depth", "1", "--branch",
                            service["branch"], url, workdir],
                           capture_output=True, text=True, timeout=300)
        return workdir if r.returncode == 0 else None
    except Exception:
        return None


def _container_logs(service) -> str:
    if not service["container"]:
        return "(no container)"
    try:
        return engine.dock().containers.get(service["container"]) \
            .logs(tail=120).decode(errors="replace")
    except Exception:
        return "(container not running)"


def investigate_sync(project_id: int, user_text: str):
    project = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
    services = db.all_("SELECT * FROM services WHERE project_id=? ORDER BY id",
                       (project_id,))
    connection = db.one("SELECT * FROM github_connections WHERE user_id=? "
                        "ORDER BY id DESC LIMIT 1", (project["user_id"],))
    if not ai.available():
        add_message(project_id, "agent",
                    "The AI engine is not configured on this instance "
                    "(OPENAI_API_KEY missing), so I can't investigate.")
        return

    workdirs: dict[str, str] = {}
    sections = []
    for s in services:
        workdir = _ensure_workdir(s, connection) if connection else None
        if workdir:
            workdirs[s["name"]] = workdir
        tree, files = (engine._snapshot(workdir) if workdir else ("(no checkout)", {}))
        dep = db.one("SELECT log, status FROM deployments WHERE service_id=? "
                     "ORDER BY id DESC", (s["id"],))
        blob = "\n".join(f"----- {s['name']}/{p} -----\n{c[:2500]}"
                         for p, c in list(files.items())[:8])
        sections.append(
            f"=== SERVICE {s['name']} ===\n"
            f"repo {s['repo_full']}@{s['branch']} · status {s['status']} · "
            f"port {s['port']} · url {engine.service_url(s['slug'])}\n"
            f"container logs (tail):\n{_container_logs(s)[-2500:]}\n"
            f"last deploy ({dep['status'] if dep else '—'}) log tail:\n"
            f"{(dep['log'][-1800:] if dep else '')}\n"
            f"file tree:\n{tree[:3000]}\nkey files:\n{blob[:16000]}\n")

    chat = history(project_id)[-12:]
    convo = "\n".join(f"{m['role']}: {m['content'][:600]}" for m in chat)

    def read_file(path: str):
        name, _, rest = path.partition("/")
        workdir = workdirs.get(name)
        if workdir is None and len(workdirs) == 1:
            workdir, rest = next(iter(workdirs.values())), path
        if not workdir or not rest:
            return None
        full = os.path.realpath(os.path.join(workdir, rest.lstrip("/")))
        if not full.startswith(os.path.realpath(workdir) + os.sep):
            return None
        try:
            return open(full, errors="replace").read(8000)
        except OSError:
            return None

    prompt = (f"PROJECT {project['name']} ({len(services)} services)\n\n"
              + "\n".join(sections)[:60000]
              + f"\n\nChat so far:\n{convo}\n\nUser's request:\n{user_text}")

    # Accumulate streaming tokens and extract the reply text as it comes
    _stream_buf: list[str] = []

    def _on_chunk(text: str):
        _stream_buf.append(text)
        accumulated = "".join(_stream_buf)
        # Extract just the reply text from the streaming JSON and publish it
        m = re.search(r'"reply"\s*:\s*"((?:[^"\\]|\\.)*)', accumulated)
        if m:
            reply_so_far = m.group(1).replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
            bus.publish(f"project:{project_id}", "stream",
                        {"text": reply_so_far})

    result = ai.chat_agent_streaming(prompt, _on_chunk, read_file)
    # Signal end of stream so the browser knows to stop the typing animation
    bus.publish(f"project:{project_id}", "stream_end", {})
    if not result or not result.get("reply"):
        add_message(project_id, "agent",
                    "I couldn't complete the investigation (AI error). Try again.")
        return
    patches = [p for p in (result.get("patches") or [])
               if isinstance(p, dict) and p.get("file") and p.get("find") is not None]
    svc_name = result.get("service")
    service = next((s for s in services if s["name"] == svc_name), None)
    if patches and service:
        add_message(project_id, "agent", result["reply"], kind="fix",
                    data={"service": service["name"], "service_id": service["id"],
                          "patches": patches[:8],
                          "commit_message": result.get("commit_message")
                          or f"Cicatrixa: fix for {service['name']}",
                          "applied": False})
    else:
        add_message(project_id, "agent", result["reply"])


# ---------- apply: patch -> verify build -> commit -> push -> redeploy ----------

def apply_fix_sync(project_id: int, message_id: int) -> tuple[int | None, str]:
    """Returns (service_id_to_deploy, status_text)."""
    msg = db.one("SELECT * FROM chat_messages WHERE id=? AND project_id=? "
                 "AND kind='fix'", (message_id, project_id))
    if not msg:
        return None, "Fix proposal not found."
    data = json.loads(msg["data"])
    if data.get("applied"):
        return None, "This fix was already applied."
    service = db.one("SELECT * FROM services WHERE id=?", (data["service_id"],))
    project = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
    connection = db.one("SELECT * FROM github_connections WHERE user_id=? "
                        "ORDER BY id DESC LIMIT 1", (project["user_id"],))
    if not service or not connection:
        return None, "Service or GitHub connection missing."

    def status(text):
        add_message(project_id, "agent", text, kind="status")

    clone = os.path.join(PUSH_ROOT, f"{service['slug']}-{int(time.time())}")
    os.makedirs(PUSH_ROOT, exist_ok=True)
    url = gh.clone_url(connection, service["repo_full"])
    try:
        status(f"⇣ cloning {service['repo_full']}@{service['branch']} for the fix…")
        r = subprocess.run(["git", "clone", "--branch", service["branch"], url, clone],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            return None, f"Clone failed: {r.stderr.strip()[-300:]}"

        applied = 0
        for patch in data["patches"]:
            rel = patch["file"].lstrip("/")
            # tolerate a service-name prefix the AI sometimes adds
            for pre in (f"{service['name']}/", f"{service['slug']}/"):
                if rel.startswith(pre) and not os.path.exists(os.path.join(clone, rel)):
                    rel = rel[len(pre):]
            full = os.path.realpath(os.path.join(clone, rel))
            if not full.startswith(os.path.realpath(clone) + os.sep):
                continue
            try:
                src = open(full, errors="replace").read()
            except OSError:
                status(f"⚠ {rel}: file not found — skipping this patch")
                continue
            if patch["find"] not in src:
                status(f"⚠ {rel}: expected code not found (repo changed?) — skipping")
                continue
            with open(full, "w") as f:
                f.write(src.replace(patch["find"], patch.get("replace") or ""))
            applied += 1
        if not applied:
            return None, "No patch could be applied — the repository has likely changed."

        # verify the patched tree still builds before touching the user's repo
        dockerfile = None
        if os.path.exists(os.path.join(clone, "Dockerfile")):
            dockerfile = "Dockerfile"
        else:
            cached = os.path.join(engine.WORK_ROOT, service["slug"], ".cx.Dockerfile")
            if os.path.exists(cached):
                shutil.copy(cached, os.path.join(clone, ".cx.Dockerfile"))
                dockerfile = ".cx.Dockerfile"
        if dockerfile:
            status("🔨 verifying the patched code still builds…")
            try:
                engine._build(clone, dockerfile, f"cx-{service['slug']}:chatfix-verify",
                              lambda _l: None)
            except RuntimeError as exc:
                return None, ("The patched code fails to build — not pushing. "
                              f"Build error: {str(exc)[-400:]}")

        msg_line = data.get("commit_message") or f"Cicatrixa: fix {service['name']}"
        env = {**os.environ, "GIT_AUTHOR_NAME": AGENT_NAME,
               "GIT_AUTHOR_EMAIL": AGENT_EMAIL, "GIT_COMMITTER_NAME": AGENT_NAME,
               "GIT_COMMITTER_EMAIL": AGENT_EMAIL}
        subprocess.run(["git", "-C", clone, "add", "-A"], capture_output=True)
        r = subprocess.run(["git", "-C", clone, "commit", "-m", msg_line],
                           capture_output=True, text=True, env=env)
        if r.returncode != 0:
            return None, f"Nothing to commit: {r.stdout.strip()[-200:]}"
        status(f"⇡ pushing to {service['repo_full']}@{service['branch']}…")
        r = subprocess.run(["git", "-C", clone, "push", "origin",
                            f"HEAD:{service['branch']}"],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            err = (r.stderr or r.stdout).strip()[-400:]
            return None, ("Push was rejected — the GitHub connection needs write access "
                          f"(App: Contents write / PAT: repo scope). Error: {err}")
        sha = subprocess.run(["git", "-C", clone, "rev-parse", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
        data["applied"] = True
        data["pushed_sha"] = sha
        db.q("UPDATE chat_messages SET data=? WHERE id=?", (json.dumps(data), message_id))
        status(f"✔ pushed {sha[:10]} ({msg_line}) — redeploying {service['name']} now")
        return service["id"], f"Fix pushed as {sha[:10]}."
    finally:
        shutil.rmtree(clone, ignore_errors=True)
        try:
            engine.dock().images.remove(f"cx-{service['slug']}:chatfix-verify",
                                        force=True)
        except Exception:
            pass
