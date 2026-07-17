"""
Deployer: builds an image from the patched source, launches it as a canary,
shifts a slice of live traffic to it via Traefik's weighted routing (file
provider, watched dynamic config), monitors the canary's error rate, then
auto-promotes to 100% or auto-rolls-back.
"""
import asyncio
import logging
import time

import docker
import httpx
import yaml

from config import (
    APP_SRC,
    CANARY_CONTAINER,
    CANARY_MAX_ERRORS,
    CANARY_URL,
    CANARY_WATCH_SEC,
    CANARY_WEIGHT,
    DOCKER_NETWORK,
    TRAEFIK_DYNAMIC,
)
from events import EventBus
from watchdog import ERROR_PATTERN

logger = logging.getLogger(__name__)


class Deployer:
    def __init__(self, bus: EventBus):
        self.bus = bus
        self._client = docker.from_env()
        self._http = httpx.AsyncClient(timeout=5.0)

    async def deploy(self) -> bool:
        """Full canary cycle. Returns True if promoted, False if rolled back."""
        tag = f"orderservice:healed-{int(time.time())}"
        loop = asyncio.get_running_loop()

        await self.bus.emit("deployer", f"Building patched image {tag}")
        await loop.run_in_executor(None, self._build, tag)

        await self.bus.emit("deployer", f"Starting canary container {CANARY_CONTAINER}")
        await loop.run_in_executor(None, self._start_canary, tag)

        if not await self._wait_healthy():
            await self.bus.emit("deployer", "Canary never became healthy — rolling back")
            await self._rollback()
            return False

        self._write_weights(stable=100 - CANARY_WEIGHT, canary=CANARY_WEIGHT)
        await self.bus.emit(
            "deployer",
            f"Canary live at {CANARY_WEIGHT}% traffic — watching error rate for {CANARY_WATCH_SEC}s",
        )

        errors = await self._watch_canary()
        if errors > CANARY_MAX_ERRORS:
            await self.bus.emit("deployer", f"Canary failed ({errors} errors) — rolling back")
            await self._rollback()
            return False

        self._write_weights(stable=0, canary=100)
        await self.bus.emit(
            "deployer",
            f"Canary promoted to 100% traffic ({errors} errors in watch window). "
            "Old version kept at weight 0 as instant-rollback path.",
        )
        return True

    # ---------- docker ----------

    def _build(self, tag: str):
        # docker-py sends APP_SRC as a client-side tarball, so the in-container
        # path works here (unlike bind mounts, which need the host path)
        self._client.images.build(path=APP_SRC, tag=tag, rm=True)

    def _start_canary(self, tag: str):
        try:
            old = self._client.containers.get(CANARY_CONTAINER)
            old.remove(force=True)
        except docker.errors.NotFound:
            pass
        self._client.containers.run(
            tag,
            name=CANARY_CONTAINER,
            network=DOCKER_NETWORK,
            detach=True,
        )

    async def _rollback(self):
        self._write_weights(stable=100, canary=0)
        loop = asyncio.get_running_loop()

        def _remove():
            try:
                self._client.containers.get(CANARY_CONTAINER).remove(force=True)
            except docker.errors.NotFound:
                pass

        await loop.run_in_executor(None, _remove)

    # ---------- canary health ----------

    async def _wait_healthy(self, timeout: float = 30.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = await self._http.get(f"{CANARY_URL}/health")
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(1.0)
        return False

    async def _watch_canary(self) -> int:
        """Poll canary health + scan its logs for errors during the watch window."""
        errors = 0
        started = time.time()
        since = int(started)
        while time.time() - started < CANARY_WATCH_SEC:
            try:
                r = await self._http.get(f"{CANARY_URL}/health")
                if r.status_code >= 500:
                    errors += 1
            except Exception:
                errors += 1

            loop = asyncio.get_running_loop()
            logs = await loop.run_in_executor(None, self._canary_logs, since)
            since = int(time.time())
            errors += sum(1 for line in logs.splitlines() if ERROR_PATTERN.search(line))

            await asyncio.sleep(3.0)
        return errors

    def _canary_logs(self, since: int) -> str:
        try:
            c = self._client.containers.get(CANARY_CONTAINER)
            return c.logs(since=since).decode("utf-8", errors="replace")
        except docker.errors.NotFound:
            return ""

    # ---------- traefik ----------

    def _write_weights(self, stable: int, canary: int):
        cfg = {
            "http": {
                "routers": {
                    "orders": {
                        "rule": "PathPrefix(`/`)",
                        "entryPoints": ["web"],
                        "service": "orders-weighted",
                    }
                },
                "services": {
                    "orders-weighted": {
                        "weighted": {
                            "services": [
                                {"name": "orders-stable", "weight": stable},
                                {"name": "orders-canary", "weight": canary},
                            ]
                        }
                    },
                    "orders-stable": {
                        "loadBalancer": {"servers": [{"url": "http://app-stable:8000"}]}
                    },
                    "orders-canary": {
                        "loadBalancer": {"servers": [{"url": "http://app-canary:8000"}]}
                    },
                }
            }
        }
        with open(TRAEFIK_DYNAMIC, "w") as f:
            yaml.safe_dump(cfg, f)
        logger.info(f"Traefik weights → stable={stable} canary={canary}")
