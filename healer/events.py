"""In-process event bus feeding the SSE dashboard and the log."""
import asyncio
import json
import time
import logging

logger = logging.getLogger(__name__)

STAGES = ["watchdog", "diagnostician", "reproducer", "fixer", "gate", "deployer", "healed"]


class EventBus:
    def __init__(self):
        self._subs: list[asyncio.Queue] = []
        self.history: list[dict] = []

    async def emit(self, stage: str, message: str, **data):
        event = {
            "ts": time.time(),
            "stage": stage,
            "message": message,
            "data": data,
        }
        self.history.append(event)
        logger.info(f"[{stage}] {message}")
        for q in list(self._subs):
            q.put_nowait(event)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._subs:
            self._subs.remove(q)

    @staticmethod
    def sse(event: dict) -> str:
        return f"data: {json.dumps(event)}\n\n"
