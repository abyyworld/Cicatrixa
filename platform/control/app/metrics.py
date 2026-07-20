"""Resource monitoring (RAM, storage) and per-user quotas.

A background collector samples docker stats and disk usage into an in-memory
cache; pages and quota checks read the cache so they stay instant.
"""
import os
import time

from . import db, dbprovision, engine

RAM_PER_CONTAINER_MB = int(os.environ.get("RAM_PER_CONTAINER_MB", "768"))
DEFAULT_QUOTA_SERVICES = int(os.environ.get("DEFAULT_QUOTA_SERVICES", "5"))
DEFAULT_QUOTA_RAM_MB = int(os.environ.get("DEFAULT_QUOTA_RAM_MB", "2048"))
DEFAULT_QUOTA_DISK_MB = int(os.environ.get("DEFAULT_QUOTA_DISK_MB", "5120"))
DEFAULT_QUOTA_DATABASES = int(os.environ.get("DEFAULT_QUOTA_DATABASES", "2"))

# instance-wide ceilings — protect the shared host itself, apply to every
# account including admins, and are editable from the admin panel (stored in
# `settings`, these are just the first-boot defaults)
DEFAULT_GLOBAL_MAX_SERVICES = int(os.environ.get("DEFAULT_GLOBAL_MAX_SERVICES", "40"))
DEFAULT_GLOBAL_MAX_DATABASES = int(os.environ.get("DEFAULT_GLOBAL_MAX_DATABASES", "10"))
DEFAULT_GLOBAL_MAX_RAM_MB = int(os.environ.get("DEFAULT_GLOBAL_MAX_RAM_MB", "6144"))

# caches refreshed by collect() from the watchdog loop
service_mem: dict[str, int] = {}    # service slug -> bytes in use
service_disk: dict[str, int] = {}   # service slug -> bytes (image + workdir)
host: dict = {}                     # host totals
_last_collect = 0.0


def universal_quota() -> dict:
    """The default quota applied to any account without an individual override —
    editable live from the admin panel (settings table), env vars are just the
    first-boot values before an admin ever changes them."""
    return {
        "services": int(db.setting("universal_quota_services", DEFAULT_QUOTA_SERVICES)),
        "ram_mb": int(db.setting("universal_quota_ram_mb", DEFAULT_QUOTA_RAM_MB)),
        "disk_mb": int(db.setting("universal_quota_disk_mb", DEFAULT_QUOTA_DISK_MB)),
        "databases": int(db.setting("universal_quota_databases", DEFAULT_QUOTA_DATABASES)),
    }


def set_universal_quota(services: int, ram_mb: int, disk_mb: int, databases: int):
    db.set_setting("universal_quota_services", str(services))
    db.set_setting("universal_quota_ram_mb", str(ram_mb))
    db.set_setting("universal_quota_disk_mb", str(disk_mb))
    db.set_setting("universal_quota_databases", str(databases))


def user_quota(user) -> dict:
    d = universal_quota()
    return {
        "services": user["quota_services"] or d["services"],
        "ram_mb": user["quota_ram_mb"] or d["ram_mb"],
        "disk_mb": user["quota_disk_mb"] or d["disk_mb"],
        "databases": d["databases"],
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
        containers += engine.dock().containers.list(
            filters={"label": "cx.database", "status": "running"})
    except Exception:
        containers = []
    for c in containers:
        slug = c.labels.get("cx.service") or c.labels.get("cx.database", "")
        try:
            s = c.stats(stream=False)
            usage = s["memory_stats"].get("usage", 0)
            usage -= s["memory_stats"].get("stats", {}).get("inactive_file", 0)
            mem[slug] = max(usage, 0)
        except Exception:
            pass
    for d in db.all_("SELECT * FROM databases WHERE status='live'"):
        disk[d["slug"]] = dbprovision.disk_bytes(d)
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
    dbs = db.all_("SELECT d.slug, d.status FROM databases d "
                  "JOIN projects p ON p.id=d.project_id WHERE p.user_id=?", (user_id,))
    running = [r["slug"] for r in rows if r["status"] in ("live", "deploying")]
    db_running = [d["slug"] for d in dbs if d["status"] == "live"]
    return {
        "services": len(rows),
        "databases": len(dbs),
        "ram_bytes": sum(service_mem.get(slug, 0) for slug in running + db_running),
        "ram_reserved_mb": len(running) * RAM_PER_CONTAINER_MB
                          + len(db_running) * dbprovision.RAM_PER_DB_MB,
        "disk_bytes": (sum(service_disk.get(r["slug"], 0) for r in rows)
                      + sum(service_disk.get(d["slug"], 0) for d in dbs)),
    }


def all_users_usage() -> list[dict]:
    out = []
    for u in db.all_("SELECT * FROM users ORDER BY id"):
        usage = user_usage(u["id"])
        out.append({"user": u, "usage": usage, "quota": user_quota(u)})
    return out


# ---------- instance-wide (global) limits ----------

def global_limits() -> dict:
    return {
        "services": int(db.setting("global_max_services", DEFAULT_GLOBAL_MAX_SERVICES)),
        "databases": int(db.setting("global_max_databases", DEFAULT_GLOBAL_MAX_DATABASES)),
        "ram_mb": int(db.setting("global_max_ram_mb", DEFAULT_GLOBAL_MAX_RAM_MB)),
    }


def set_global_limits(services: int, databases: int, ram_mb: int):
    db.set_setting("global_max_services", str(services))
    db.set_setting("global_max_databases", str(databases))
    db.set_setting("global_max_ram_mb", str(ram_mb))


def global_usage() -> dict:
    services = db.one("SELECT COUNT(*) c FROM services")["c"]
    databases = db.one("SELECT COUNT(*) c FROM databases")["c"]
    active_services = db.one(
        "SELECT COUNT(*) c FROM services WHERE status IN ('live','deploying')")["c"]
    live_databases = db.one("SELECT COUNT(*) c FROM databases WHERE status='live'")["c"]
    return {
        "services": services,
        "databases": databases,
        "ram_reserved_mb": active_services * RAM_PER_CONTAINER_MB
                          + live_databases * dbprovision.RAM_PER_DB_MB,
    }


# ---------- growth stats (admin dashboard / funnel numbers) ----------

def growth_stats() -> dict:
    """Signup/deploy funnel counts for the admin page."""
    week_ago = time.time() - 7 * 86400
    c = lambda sql, *a: db.one(sql, a)["c"]
    return {
        "users": c("SELECT COUNT(*) c FROM users"),
        "users_verified": c("SELECT COUNT(*) c FROM users WHERE email_verified=1"),
        "users_7d": c("SELECT COUNT(*) c FROM users WHERE created_at>?", week_ago),
        "projects": c("SELECT COUNT(*) c FROM projects"),
        "deploys": c("SELECT COUNT(*) c FROM deployments"),
        "deploys_7d": c("SELECT COUNT(*) c FROM deployments WHERE created_at>?", week_ago),
        "deploys_ok": c("SELECT COUNT(*) c FROM deployments WHERE status='success'"),
        "medic_fixes": c("SELECT COUNT(*) c FROM deployments WHERE trigger='chat-fix'"),
        "invites_sent": c("SELECT COUNT(*) c FROM invites"),
        "invites_used": c("SELECT COUNT(*) c FROM invites WHERE status='used'"),
    }


# ---------- quota enforcement ----------

def check_service_count(user, extra: int = 1) -> str | None:
    """Return an error string if adding `extra` services would exceed the quota."""
    limits = global_limits()
    total = db.one("SELECT COUNT(*) c FROM services")["c"]
    if total + extra > limits["services"]:
        return (f"Instance-wide service limit reached: {total}/{limits['services']} "
                f"used across all accounts. Ask an admin to raise the instance limit.")
    if user["is_admin"]:
        return None
    quota = user_quota(user)
    current = db.one("SELECT COUNT(*) c FROM services s JOIN projects p "
                     "ON p.id=s.project_id WHERE p.user_id=?", (user["id"],))["c"]
    if current + extra > quota["services"]:
        return (f"Service limit reached: {current}/{quota['services']} used, "
                f"tried to add {extra}. Remove a service or ask the admin to raise "
                f"your quota.")
    return None


def check_database_count(user, extra: int = 1) -> str | None:
    """Return an error string if adding `extra` databases would exceed the quota."""
    limits = global_limits()
    total = db.one("SELECT COUNT(*) c FROM databases")["c"]
    if total + extra > limits["databases"]:
        return (f"Instance-wide database limit reached: {total}/{limits['databases']} "
                f"used across all accounts. Ask an admin to raise the instance limit.")
    if user["is_admin"]:
        return None
    quota = user_quota(user)
    current = db.one("SELECT COUNT(*) c FROM databases d JOIN projects p "
                     "ON p.id=d.project_id WHERE p.user_id=?", (user["id"],))["c"]
    if current + extra > quota["databases"]:
        return (f"Database limit reached: {current}/{quota['databases']} used. "
                f"Remove a database or ask the admin to raise your quota.")
    return None


def check_deploy_quota(user, service) -> str | None:
    """RAM + disk gate, called at the start of every deploy. The instance-wide RAM
    ceiling applies to everyone, admins included — it protects the shared host,
    not fairness between accounts."""
    limits = global_limits()
    usage = global_usage()
    if usage["ram_reserved_mb"] > limits["ram_mb"]:
        return (f"Instance-wide RAM limit reached: {usage['ram_reserved_mb']}MB "
                f"reserved > {limits['ram_mb']}MB. Ask an admin to raise it, or free "
                f"up capacity by stopping a service.")
    if user["is_admin"]:
        return None
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
