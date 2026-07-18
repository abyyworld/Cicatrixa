"""Resource monitoring (RAM, storage) and per-user quotas.

A background collector samples docker stats and disk usage into an in-memory
cache; pages and quota checks read the cache so they stay instant.
"""
import os
import time

from . import db, engine

RAM_PER_CONTAINER_MB = int(os.environ.get("RAM_PER_CONTAINER_MB", "768"))
DEFAULT_QUOTA_SERVICES = int(os.environ.get("DEFAULT_QUOTA_SERVICES", "5"))
DEFAULT_QUOTA_RAM_MB = int(os.environ.get("DEFAULT_QUOTA_RAM_MB", "2048"))
DEFAULT_QUOTA_DISK_MB = int(os.environ.get("DEFAULT_QUOTA_DISK_MB", "5120"))

# caches refreshed by collect() from the watchdog loop
service_mem: dict[str, int] = {}    # service slug -> bytes in use
service_disk: dict[str, int] = {}   # service slug -> bytes (image + workdir)
host: dict = {}                     # host totals
_last_collect = 0.0


def user_quota(user) -> dict:
    return {
        "services": user["quota_services"] or DEFAULT_QUOTA_SERVICES,
        "ram_mb": user["quota_ram_mb"] or DEFAULT_QUOTA_RAM_MB,
        "disk_mb": user["quota_disk_mb"] or DEFAULT_QUOTA_DISK_MB,
    }


def _dir_size(path: str) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def collect():
    """Refresh all caches. Called from the watchdog loop; cheap enough for 60s."""
    global _last_collect
    mem, disk = {}, {}
    try:
        containers = engine.dock().containers.list(
            filters={"label": "cx.service", "status": "running"})
    except Exception:
        containers = []
    for c in containers:
        slug = c.labels.get("cx.service", "")
        try:
            s = c.stats(stream=False)
            usage = s["memory_stats"].get("usage", 0)
            usage -= s["memory_stats"].get("stats", {}).get("inactive_file", 0)
            mem[slug] = max(usage, 0)
        except Exception:
            pass
    for s in db.all_("SELECT slug FROM services"):
        slug = s["slug"]
        size = 0
        try:
            seen = set()
            for img in engine.dock().images.list(name=f"cx-{slug}"):
                if img.id not in seen:
                    seen.add(img.id)
                    size += img.attrs.get("Size", 0)
        except Exception:
            pass
        size += _dir_size(os.path.join(engine.WORK_ROOT, slug))
        disk[slug] = size

    service_mem.clear(); service_mem.update(mem)
    service_disk.clear(); service_disk.update(disk)
    host.update(_host_stats())
    _last_collect = time.time()


def _host_stats() -> dict:
    stats = {}
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                info[key] = int(rest.split()[0]) * 1024  # kB -> bytes
        stats["ram_total"] = info.get("MemTotal", 0)
        stats["ram_used"] = info.get("MemTotal", 0) - info.get("MemAvailable", 0)
    except Exception:
        pass
    try:
        import shutil
        du = shutil.disk_usage("/data")
        stats["disk_total"], stats["disk_used"] = du.total, du.total - du.free
    except Exception:
        pass
    return stats


def ensure_fresh():
    if time.time() - _last_collect > 180:
        collect()


# ---------- per-user aggregation ----------

def user_usage(user_id: int) -> dict:
    """Current usage for one user, from the caches."""
    rows = db.all_("SELECT s.slug, s.status FROM services s "
                   "JOIN projects p ON p.id=s.project_id WHERE p.user_id=?",
                   (user_id,))
    running = [r["slug"] for r in rows if r["status"] in ("live", "deploying")]
    return {
        "services": len(rows),
        "ram_bytes": sum(service_mem.get(slug, 0) for slug in running),
        "ram_reserved_mb": len(running) * RAM_PER_CONTAINER_MB,
        "disk_bytes": sum(service_disk.get(r["slug"], 0) for r in rows),
    }


def all_users_usage() -> list[dict]:
    out = []
    for u in db.all_("SELECT * FROM users ORDER BY id"):
        usage = user_usage(u["id"])
        out.append({"user": u, "usage": usage, "quota": user_quota(u)})
    return out


# ---------- quota enforcement ----------

def check_service_count(user, extra: int = 1) -> str | None:
    """Return an error string if adding `extra` services would exceed the quota."""
    quota = user_quota(user)
    current = db.one("SELECT COUNT(*) c FROM services s JOIN projects p "
                     "ON p.id=s.project_id WHERE p.user_id=?", (user["id"],))["c"]
    if current + extra > quota["services"]:
        return (f"Service limit reached: {current}/{quota['services']} used, "
                f"tried to add {extra}. Remove a service or ask the admin to raise "
                f"your quota.")
    return None


def check_deploy_quota(user, service) -> str | None:
    """RAM + disk gate, called at the start of every deploy."""
    quota = user_quota(user)
    active = db.one(
        "SELECT COUNT(*) c FROM services s JOIN projects p ON p.id=s.project_id "
        "WHERE p.user_id=? AND (s.status IN ('live','deploying') OR s.id=?)",
        (user["id"], service["id"]))["c"]
    if active * RAM_PER_CONTAINER_MB > quota["ram_mb"]:
        return (f"RAM quota exceeded: {active} running services × "
                f"{RAM_PER_CONTAINER_MB}MB reserved > {quota['ram_mb']}MB limit.")
    ensure_fresh()
    disk_mb = user_usage(user["id"])["disk_bytes"] // (1024 * 1024)
    if disk_mb > quota["disk_mb"]:
        return (f"Storage quota exceeded: {disk_mb}MB used > "
                f"{quota['disk_mb']}MB limit. Delete a service or old projects.")
    return None


def fmt_bytes(n: int | float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit in ("B", "KB") else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"
