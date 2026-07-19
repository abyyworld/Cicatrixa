"""On-demand PostgreSQL databases: one click, a live container, wired into every
sibling service in the project as DATABASE_URL — no manual connection-string wiring."""
import os
import re
import secrets
import time

import docker.errors

from . import bus, db, engine

POSTGRES_IMAGE = "postgres:16-alpine"
RAM_PER_DB_MB = int(os.environ.get("RAM_PER_DB_MB", "512"))


def _env_key(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).upper().strip("_") or "DB"


def make_slug(project_slug: str, name: str) -> str:
    base = re.sub(r"[^a-z0-9-]+", "-", f"{project_slug}-{name}".lower()).strip("-")[:40] or "db"
    slug, n = base, 1
    while (db.one("SELECT 1 FROM services WHERE slug=?", (slug,))
           or db.one("SELECT 1 FROM databases WHERE slug=?", (slug,))
           or db.one("SELECT 1 FROM projects WHERE slug=?", (slug,))):
        n += 1
        slug = f"{base}-{n}"
    return slug


def create(project, name: str) -> int:
    slug = make_slug(project["slug"], name)
    db_name = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")[:32] or "app"
    password = secrets.token_urlsafe(18)
    did = db.q(
        "INSERT INTO databases(project_id,name,slug,db_name,db_user,db_password,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (project["id"], name, slug, db_name, db_name, password, db.now())).lastrowid
    _start(did)
    return did


def _log(project_id: int, name: str, line: str):
    bus.publish(f"project:{project_id}", "log",
               {"line": f"[{time.strftime('%H:%M:%S')}] ⟦db:{name}⟧ {line}"})


def _start(database_id: int):
    row = db.one("SELECT * FROM databases WHERE id=?", (database_id,))
    project = db.one("SELECT * FROM projects WHERE id=?", (row["project_id"],))
    slug = row["slug"]
    _log(project["id"], row["name"], f"provisioning postgres 16 ({slug})…")
    try:
        client = engine.dock()
        try:
            client.images.get(POSTGRES_IMAGE)
        except docker.errors.ImageNotFound:
            _log(project["id"], row["name"], f"pulling {POSTGRES_IMAGE}…")
            client.images.pull(POSTGRES_IMAGE)
        for old in client.containers.list(all=True, filters={"label": f"cx.database={slug}"}):
            old.remove(force=True)
        volume = f"cxdb-{slug}"
        container = client.containers.create(
            POSTGRES_IMAGE, name=f"cx-{slug}",
            labels={"cx.project": str(project["id"]), "cx.database": slug,
                   "cx.user": str(project["user_id"])},
            environment={"POSTGRES_DB": row["db_name"], "POSTGRES_USER": row["db_user"],
                        "POSTGRES_PASSWORD": row["db_password"]},
            volumes={volume: {"bind": "/var/lib/postgresql/data", "mode": "rw"}},
            mem_limit=f"{RAM_PER_DB_MB}m", nano_cpus=500_000_000,
            restart_policy={"Name": "unless-stopped"})
        try:
            client.networks.get("bridge").disconnect(container)
        except Exception:
            pass
        client.networks.get(engine.NETWORK).connect(container, aliases=[slug])
        container.start()
        db.q("UPDATE databases SET container=?, status='live' WHERE id=?",
             (container.name, database_id))
        _log(project["id"], row["name"], f"✔ live — internal host {slug}:5432")
    except Exception as exc:
        db.q("UPDATE databases SET status='failed' WHERE id=?", (database_id,))
        _log(project["id"], row["name"], f"✖ failed to start: {exc}")


def restart(database_id: int):
    """Used by the watchdog and the manual restart button — data lives in the
    volume, so a restart (or even a full recreate) never loses it."""
    _start(database_id)


def stop(database_id: int):
    row = db.one("SELECT * FROM databases WHERE id=?", (database_id,))
    if row and row["container"]:
        try:
            c = engine.dock().containers.get(row["container"])
            c.stop(timeout=8)
            c.remove(force=True)
        except Exception:
            pass
    db.q("UPDATE databases SET status='stopped', container=NULL WHERE id=?", (database_id,))


def delete(database_id: int):
    row = db.one("SELECT * FROM databases WHERE id=?", (database_id,))
    if not row:
        return
    stop(database_id)
    try:
        engine.dock().volumes.get(f"cxdb-{row['slug']}").remove(force=True)
    except Exception:
        pass
    db.q("DELETE FROM databases WHERE id=?", (database_id,))


def connection_url(database) -> str:
    return (f"postgresql://{database['db_user']}:{database['db_password']}@"
           f"{database['slug']}:5432/{database['db_name']}")


def sibling_env(project_id: int) -> dict:
    """Merged into every app-service container's environment in this project."""
    env = {}
    rows = db.all_("SELECT * FROM databases WHERE project_id=? AND status='live'",
                   (project_id,))
    for i, row in enumerate(rows):
        url = connection_url(row)
        env[f"{_env_key(row['name'])}_DATABASE_URL"] = url
        if i == 0:
            env.setdefault("DATABASE_URL", url)
    return env


def disk_bytes(database) -> int:
    """Approximate on-disk size of the data volume, via `du` inside the container."""
    try:
        c = engine.dock().containers.get(database["container"])
        out = c.exec_run(["du", "-sb", "/var/lib/postgresql/data"])
        return int(out.output.decode().split()[0])
    except Exception:
        return 0
