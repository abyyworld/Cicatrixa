"""In-process pub/sub for streaming deploy logs and project events over SSE."""
import asyncio
import json
import time
from collections import defaultdict

_subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
_loop: asyncio.AbstractEventLoop | None = None


def attach_loop(loop: asyncio.AbstractEventLoop):
    global _loop
    _loop = loop


def publish(channel: str, event: str, data: dict):
    """Thread-safe publish (deploy pipeline runs in worker threads)."""
    msg = {"event": event, "data": data, "ts": time.time()}
    if _loop is None:
        return
    _loop.call_soon_threadsafe(_fanout, channel, msg)


def _fanout(channel: str, msg: dict):
    for queue in list(_subscribers.get(channel, ())):
        if queue.qsize() < 1000:
            queue.put_nowait(msg)


async def subscribe(channel: str):
    queue: asyncio.Queue = asyncio.Queue()
    _subscribers[channel].add(queue)
    try:
        while True:
            msg = await queue.get()
            yield f"event: {msg['event']}\ndata: {json.dumps(msg['data'])}\n\n"
    finally:
        _subscribers[channel].discard(queue)
