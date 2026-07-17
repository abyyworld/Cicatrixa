"""
Watchdog: polls health endpoints + tails container logs.
Emits a CrashEvent when error spike or crash is detected.
"""
import asyncio
import re
import time
import logging
from dataclasses import dataclass, field
from collections import deque
from typing import Callable, Awaitable

import httpx
import docker

logger = logging.getLogger(__name__)

ERROR_PATTERN = re.compile(
    r"(Traceback|Error:|Exception:|CRITICAL|500 Internal)", re.IGNORECASE
)
SPIKE_WINDOW_SEC = 30
SPIKE_THRESHOLD = 3  # errors in window = trigger


@dataclass
class CrashEvent:
    container_id: str
    container_name: str
    timestamp: float
    error_lines: list[str]
    traceback: str
    health_url: str


class Watchdog:
    def __init__(
        self,
        target_container: str,
        health_url: str,
        on_crash: Callable[[CrashEvent], Awaitable[None]],
        poll_interval: float = 5.0,
    ):
        self.target_container = target_container
        self.health_url = health_url
        self.on_crash = on_crash
        self.poll_interval = poll_interval
        self._client = docker.from_env()
        self._http = httpx.AsyncClient(timeout=5.0)
        self._error_times: deque[float] = deque()
        self._recent_errors: deque[str] = deque(maxlen=20)
        self._last_traceback: list[str] = []
        self._capturing_tb = False
        self._triggered = False
        self._loop: asyncio.AbstractEventLoop | None = None

    async def run(self):
        logger.info(f"Watchdog started → {self.target_container} | health: {self.health_url}")
        await asyncio.gather(
            self._poll_health(),
            self._tail_logs(),
        )

    async def _poll_health(self):
        consecutive_failures = 0
        while True:
            try:
                r = await self._http.get(self.health_url)
                if r.status_code >= 500:
                    consecutive_failures += 1
                    logger.warning(f"Health check HTTP {r.status_code} (#{consecutive_failures})")
                    if consecutive_failures >= 3:
                        await self._fire_event("health check returned 500 three times in a row")
                else:
                    consecutive_failures = 0
            except Exception as exc:
                consecutive_failures += 1
                logger.warning(f"Health check unreachable: {exc} (#{consecutive_failures})")
                if consecutive_failures >= 3:
                    await self._fire_event(f"health endpoint unreachable: {exc}")
            await asyncio.sleep(self.poll_interval)

    async def _tail_logs(self):
        self._loop = asyncio.get_running_loop()
        await self._loop.run_in_executor(None, self._tail_logs_sync)

    def _tail_logs_sync(self):
        try:
            container = self._client.containers.get(self.target_container)
        except docker.errors.NotFound:
            logger.error(f"Container not found: {self.target_container}")
            return

        logger.info(f"Tailing logs for {self.target_container}")
        for raw in container.logs(stream=True, follow=True, tail=0):
            line = raw.decode("utf-8", errors="replace").rstrip()
            self._process_log_line(line)

    def _process_log_line(self, line: str):
        now = time.time()

        # Accumulate traceback
        if "Traceback (most recent call last)" in line:
            self._capturing_tb = True
            self._last_traceback = [line]
        elif self._capturing_tb:
            self._last_traceback.append(line)
            if line and not line.startswith(" ") and "Error" in line:
                self._capturing_tb = False

        if ERROR_PATTERN.search(line):
            self._error_times.append(now)
            self._recent_errors.append(line)

        # Purge old entries
        cutoff = now - SPIKE_WINDOW_SEC
        while self._error_times and self._error_times[0] < cutoff:
            self._error_times.popleft()

        if len(self._error_times) >= SPIKE_THRESHOLD and self._loop is not None:
            tb = "\n".join(self._last_traceback) if self._last_traceback else line
            asyncio.run_coroutine_threadsafe(self._fire_event_from_tb(tb), self._loop)

    async def _fire_event_from_tb(self, traceback: str):
        await self._fire_event(traceback)

    async def _fire_event(self, traceback: str):
        if self._triggered:
            return
        self._triggered = True
        try:
            container = self._client.containers.get(self.target_container)
            event = CrashEvent(
                container_id=container.id,
                container_name=self.target_container,
                timestamp=time.time(),
                error_lines=list(self._recent_errors),
                traceback=traceback,
                health_url=self.health_url,
            )
            logger.critical(f"CRASH EVENT fired for {self.target_container}")
            await self.on_crash(event)
        except Exception as exc:
            logger.error(f"Error firing crash event: {exc}")
        finally:
            # Cool-down: don't re-trigger for 120 s
            await asyncio.sleep(120)
            self._triggered = False
            self._error_times.clear()
