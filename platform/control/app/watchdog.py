"""Background watchdogs: GitHub update polling + container health monitoring."""
import asyncio
import os

from . import bus, db, engine, gh, metrics

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "180"))
HEALTH_INTERVAL = int(os.environ.get("HEALTH_INTERVAL", "60"))
METRICS_INTERVAL = int(os.environ.get("METRICS_INTERVAL", "60"))
_restart_strikes: dict[int, int] = {}


async def run_forever():
    await asyncio.gather(_poll_loop(), _health_loop(), _metrics_loop())


async def _metrics_loop():
    while True:
        try:
            await asyncio.to_thread(metrics.collect)
        except Exception:
            pass
        await asyncio.sleep(METRICS_INTERVAL)


# ---- watchdog 1: GitHub updates (webhooks are instant; this is the safety net) ----

async def _poll_loop():
    while True:
        try:
            await asyncio.to_thread(_check_repos_sync)
        except Exception:
            pass
        await asyncio.sleep(POLL_INTERVAL)


def _check_repos_sync():
    stale = []
    for s in db.all_(
            "SELECT s.*, p.user_id AS owner, p.id AS pid FROM services s "
            "JOIN projects p ON p.id=s.project_id "
            "WHERE p.autodeploy=1 AND s.status IN ('live','failed')"):
        connection = db.one("SELECT * FROM github_connections WHERE user_id=? "
                            "ORDER BY id DESC LIMIT 1", (s["owner"],))
        if not connection:
            continue
        sha = gh.head_sha(connection, s["repo_full"], s["branch"])
        if sha and s["last_sha"] and sha != s["last_sha"]:
            stale.append((s["id"], s["pid"], s["name"], sha))
    for sid, pid, name, sha in stale:
        bus.publish(f"project:{pid}", "log",
                    {"line": f"⟳ watchdog: new commit {sha[:10]} on {name} — redeploying"})
        asyncio.run_coroutine_threadsafe(engine.deploy(sid, "poll"), _loop())


# ---- watchdog 2: container health ----

async def _health_loop():
    while True:
        try:
            await asyncio.to_thread(_check_health_sync)
        except Exception:
            pass
        await asyncio.sleep(HEALTH_INTERVAL)


def _check_health_sync():
    for s in db.all_("SELECT * FROM services WHERE status='live' "
                     "AND container IS NOT NULL"):
        chan = f"project:{s['project_id']}"
        try:
            c = engine.dock().containers.get(s["container"])
            if c.status == "running":
                _restart_strikes.pop(s["id"], None)
                continue
            strikes = _restart_strikes.get(s["id"], 0) + 1
            _restart_strikes[s["id"]] = strikes
            if strikes <= 2:
                bus.publish(chan, "log",
                            {"line": f"⚠ watchdog: {s['name']} container {c.status} — "
                                     f"restarting (attempt {strikes}/2)"})
                c.restart(timeout=10)
            else:
                bus.publish(chan, "log",
                            {"line": f"✖ watchdog: {s['name']} keeps dying — rebuilding "
                                     "from source"})
                _restart_strikes.pop(s["id"], None)
                asyncio.run_coroutine_threadsafe(engine.deploy(s["id"], "heal"), _loop())
        except Exception:
            # container vanished entirely -> mark failed (poll/manual can redeploy)
            db.q("UPDATE services SET status='failed' WHERE id=?", (s["id"],))
            engine.refresh_project_status(s["project_id"])
            project = db.one("SELECT * FROM projects WHERE id=?", (s["project_id"],))
            if project:
                engine._notify_failure(project, s)


_main_loop: asyncio.AbstractEventLoop | None = None


def attach_loop(loop: asyncio.AbstractEventLoop):
    global _main_loop
    _main_loop = loop


def _loop() -> asyncio.AbstractEventLoop:
    assert _main_loop is not None
    return _main_loop
