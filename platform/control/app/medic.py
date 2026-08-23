"""Chat medic: the user asks for an error check or a fix in chat; the AI investigates
the live containers and the code, proposes exact patches, and — once the user approves —
commits them to the user's GitHub repository and redeploys."""
import json
import os
import re
import shutil
import subprocess
import time

from . import (ai, bus, db, engine, fingerprint, flywheel, gh, patching,
               verify)

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

    # Hook 1 + 3: the observation exists before the model is asked for anything,
    # and the library is queried first. Lookup after generation would measure
    # nothing — the hit rate is only meaningful if a hit could have replaced the
    # generation.
    observation_id = None
    try:
        observation_id = flywheel.observe(project["user_id"], project_id=project_id,
                                          trigger="crash")
        flywheel.lookup(observation_id)     # nothing known yet: a miss, recorded as one
    except Exception:
        observation_id = None               # instrumentation must never block a fix

    result = ai.chat_agent_streaming(prompt, _on_chunk, read_file)
    # Signal end of stream so the browser knows to stop the typing animation
    bus.publish(f"project:{project_id}", "stream_end", {})
    if not result or not result.get("reply"):
        add_message(project_id, "agent",
                    "I couldn't complete the investigation (AI error). Try again.")
        return
    raw_patches = [p for p in (result.get("patches") or [])
                  if isinstance(p, dict) and p.get("file") and p.get("find") is not None]
    svc_name = result.get("service")
    service = next((s for s in services if s["name"] == svc_name), None)
    patches = _verify_patches(raw_patches, service, workdirs) if service else []
    reply = result["reply"]
    if raw_patches and not patches:
        # the AI proposed a fix, but every "find" string mismatched the real file —
        # never show an Apply button that's guaranteed to no-op on click
        reply += ("\n\n(A fix was drafted, but it didn't match the file precisely enough "
                 "to apply safely — try asking again, maybe with more specific detail.)")
    if patches and service:
        add_message(project_id, "agent", reply, kind="fix",
                    data={"service": service["name"], "service_id": service["id"],
                          "observation_id": observation_id,
                          "patches": patches[:8],
                          "commit_message": result.get("commit_message")
                          or f"Cicatrixa: fix for {service['name']}",
                          "applied": False})
    else:
        add_message(project_id, "agent", reply)


def _verify_patches(patches: list[dict], service, workdirs: dict) -> list[dict]:
    """Drop any patch whose 'find' text doesn't actually occur in the file, so the
    chat never offers an Apply button that's guaranteed to no-op at apply time."""
    workdir = workdirs.get(service["name"])
    if workdir is None and len(workdirs) == 1:
        workdir = next(iter(workdirs.values()))
    if not workdir:
        return []
    verified = []
    for patch in patches:
        rel = patch["file"].lstrip("/")
        for pre in (f"{service['name']}/", f"{service['slug']}/"):
            if rel.startswith(pre):
                rel = rel[len(pre):]
        full = os.path.realpath(os.path.join(workdir, rel))
        if not full.startswith(os.path.realpath(workdir) + os.sep):
            continue
        try:
            src = open(full, errors="replace").read()
        except OSError:
            continue
        if patching.check(src, patch) is patching.OK:
            verified.append(patch)
    return verified


def _record_fix(observation_id, clone: str, changed: dict, service):
    """Hook 4. Fingerprint the changed call site so a later incident in another
    repo can match this one without either repo's source being stored.

    Python only, and silent when it cannot be computed — a wrong fingerprint
    produces confident false matches, which is worse than no fingerprint.
    """
    if not observation_id:
        return
    try:
        symbol = None
        fp_hash = None
        for rel, (_before, after) in changed.items():
            if not rel.endswith(".py"):
                continue
            call = _first_vendor_call(after)
            if call is not None:
                symbol, fp_hash = call
                break
        flywheel.set_root_cause(observation_id, symbol_path=symbol,
                                break_kind="unknown")
        flywheel.set_fix(observation_id, fingerprint=fp_hash)
    except Exception:
        pass


def _first_vendor_call(source: str):
    """(dotted symbol, fingerprint) of the first non-local call in the file, or
    None. Best-effort: without a dependency graph we cannot tell a vendor call
    from a local one, so break_kind stays `unknown` and the symbol is a hint,
    never an assertion."""
    try:
        import libcst as cst
        tree = cst.parse_module(source)
    except Exception:
        return None
    found = []

    class V(cst.CSTVisitor):
        def visit_Call(self, node):
            name = fingerprint.dotted_name(node.func)
            if name and "." in name:
                found.append((name, fingerprint.of_call(node)))

    tree.visit(V())
    return found[0] if found else None


def _deliver(clone, service, connection, sha, msg_line, verification,
             observation_id, status) -> tuple[dict, str | None]:
    """Open a pull request when the service is in PR mode, otherwise push to the
    tracked branch. Returns (delivery_info, error).

    PR mode is per service and defaults on, but an installation that predates the
    pull_requests permission cannot open one — GitHub keeps existing installs on
    the permissions they accepted. Rather than fail the fix, we fall back to the
    old behaviour and say so.
    """
    repo, branch = service["repo_full"], service["branch"]

    def push(refspec: str) -> str | None:
        r = subprocess.run(["git", "-C", clone, "push", "origin", refspec],
                           capture_output=True, text=True, timeout=120)
        return None if r.returncode == 0 else (r.stderr or r.stdout).strip()[-400:]

    if flywheel.pr_mode_enabled(service):
        head = f"cicatrixa/fix-{sha[:10]}"
        status(f"⇡ pushing {head} and opening a pull request on {repo}…")
        err = push(f"HEAD:refs/heads/{head}")
        if err:
            return {}, ("Push was rejected — the GitHub connection needs write access "
                        f"(App: Contents write / PAT: repo scope). Error: {err}")
        try:
            pr = gh.open_pull_request(
                connection, repo, head=head, base=branch, title=msg_line,
                body=_pr_body(msg_line, verification))
            status(f"✔ opened {pr['url']} — review and merge when you're happy")
            if observation_id:
                flywheel.set_pr_opened(observation_id, repo_full=repo,
                                       number=pr["number"], url=pr["url"], branch=head)
            return {"mode": "pull_request", "branch": head, **pr}, None
        except gh.NoPullRequestPermission as exc:
            status(f"⚠ {exc} Falling back to pushing {branch} directly.")
        except Exception as exc:
            status(f"⚠ could not open a pull request ({str(exc)[:160]}) — "
                   f"falling back to pushing {branch} directly.")

    status(f"⇡ pushing to {repo}@{branch}…")
    err = push(f"HEAD:{branch}")
    if err:
        return {}, ("Push was rejected — the GitHub connection needs write access "
                    f"(App: Contents write / PAT: repo scope). Error: {err}")
    return {"mode": "branch", "branch": branch}, None


def _pr_body(msg_line: str, verification: dict) -> str:
    """State exactly what was and was not proven. A PR that overclaims is worse
    than one that says it proved nothing."""
    level = (verification or {}).get("level", verify.UNVERIFIED)
    covered = (verification or {}).get("covered_changed_lines") or {}
    lines = [msg_line, "", f"**Verification: `{level}`**", ""]
    if level == verify.UNVERIFIED:
        if not (verification or {}).get("suite_ran"):
            lines.append("No test suite was run against this change, so nothing here is "
                         "proven. Review it as you would any untested patch.")
        elif not (verification or {}).get("suite_passed"):
            lines.append("The suite does not pass on this change.")
        else:
            lines.append("The suite passes, but no test executes the lines this patch "
                         "changed — so a green suite proves only that nothing already "
                         "covered broke. Treat this as unverified.")
    else:
        n = sum(len(v) for v in covered.values())
        lines.append(f"The suite passes and {n} of the changed line(s) are executed by "
                     f"your existing tests:")
        lines += [f"- `{path}` lines {', '.join(str(x) for x in nums)}"
                  for path, nums in sorted(covered.items())]
    lines += ["", "---", "Opened by Cicatrixa."]
    return "\n".join(lines)


def _verify_fix(clone: str, changed: dict, image: str | None, status):
    """Run the repo's own suite against the patched tree and report honestly.

    Never raises: verification failing must degrade the claim, not block a fix
    or break the push. A failure here means we learned nothing, which is
    unverified_no_coverage — the same as having no tests.
    """
    result = {"level": verify.UNVERIFIED, "detail": "", "suite_ran": False}
    evidence = None
    try:
        if not changed:
            result["detail"] = "no files changed"
            return result, evidence
        if not verify.detect_pytest(clone):
            status("🧪 no Python test suite in this repo — recording the fix as "
                   "unverified (no coverage)")
            result["detail"] = "no pytest suite detected"
            return result, evidence
        if not image:
            result["detail"] = "no image available to run the suite in"
            return result, evidence

        status("🧪 running your test suite against the patched code…")
        evidence = verify.collect(clone, changed, run=engine.test_runner(image, clone))
        level = verify.level_for(evidence)
        verify.assert_supported(level, evidence)   # belt and braces

        covered = evidence.covered_changed_lines()
        result.update(level=level, suite_ran=evidence.suite_ran,
                      suite_passed=evidence.suite_passed,
                      changed_lines={k: sorted(v) for k, v in evidence.changed_lines.items()},
                      covered_changed_lines={k: sorted(v) for k, v in covered.items()})
        if not evidence.suite_passed:
            status("🧪 the suite does not pass on the patched code — recording as "
                   "unverified")
        elif level == verify.UNVERIFIED:
            status("🧪 suite green, but no test exercises the lines we changed — "
                   "recording as unverified (no coverage), not as verified")
        else:
            n = sum(len(v) for v in covered.values())
            status(f"✅ suite green and {n} of the changed line(s) are exercised by "
                   f"your tests — recorded as {level}")
        result["detail"] = f"suite_passed={evidence.suite_passed}"
    except Exception as exc:
        status(f"🧪 could not run the suite ({str(exc)[:200]}) — recording as unverified")
        result["detail"] = f"verification error: {str(exc)[:300]}"
    return result, evidence


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
        changed: dict[str, tuple[str, str]] = {}
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
            reason = patching.check(src, patch)
            if reason is not None:
                status(f"⚠ {rel}: {reason} — skipping this patch")
                continue
            patched = patching.apply(src, patch)
            with open(full, "w") as f:
                f.write(patched)
            changed[rel] = (src, patched)
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

        observation_id = data.get("observation_id")
        _record_fix(observation_id, clone, changed, service)      # hook 4

        # The build proves it compiles. Only the suite — and only coverage of the
        # lines we actually changed — proves the fix does anything.
        verification, evidence = _verify_fix(
            clone, changed,
            f"cx-{service['slug']}:chatfix-verify" if dockerfile else None, status)
        data["verification"] = verification
        if observation_id and evidence is not None:
            try:
                flywheel.set_verification(observation_id, evidence)    # hook 5
            except Exception:
                pass

        msg_line = data.get("commit_message") or f"Cicatrixa: fix {service['name']}"
        env = {**os.environ, "GIT_AUTHOR_NAME": AGENT_NAME,
               "GIT_AUTHOR_EMAIL": AGENT_EMAIL, "GIT_COMMITTER_NAME": AGENT_NAME,
               "GIT_COMMITTER_EMAIL": AGENT_EMAIL}
        subprocess.run(["git", "-C", clone, "add", "-A"], capture_output=True)
        r = subprocess.run(["git", "-C", clone, "commit", "-m", msg_line],
                           capture_output=True, text=True, env=env)
        if r.returncode != 0:
            return None, f"Nothing to commit: {r.stdout.strip()[-200:]}"
        sha = subprocess.run(["git", "-C", clone, "rev-parse", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
        pushed, err = _deliver(clone, service, connection, sha, msg_line,
                               verification, observation_id, status)
        if err:
            return None, err
        data["applied"] = True
        data["pushed_sha"] = sha
        data["delivery"] = pushed
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
