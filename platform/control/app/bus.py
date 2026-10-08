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


# Seconds of quiet before a comment line goes out. Behind Cloudflare (tunnel mode)
# a response that sends nothing for ~100 s is cut with a 524, and EventSource does
# not reconnect after an error status — live logs and medic replies just stop.
HEARTBEAT = 20


async def subscribe(channel: str, alive=None):
    """Events on a channel as SSE text. `alive` (async, returns bool) is asked at
    every heartbeat: the stream ends when it says no. The server cannot rely on
    noticing a departed client by itself — with this uvicorn, writing to one is a
    silent no-op — so without it every closed tab kept a subscriber, and its
    heartbeat, until the process restarted (and held up every shutdown)."""
    queue: asyncio.Queue = asyncio.Queue()
    _subscribers[channel].add(queue)
    try:
        # Something at once, so the response starts now rather than at the first
        # event (a compressing proxy holds the headers until the first byte).
        yield ": connected\n\n"
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), HEARTBEAT)
            except asyncio.TimeoutError:
                if alive is not None and not await alive():
                    return
                yield ": keep-alive\n\n"
                continue
            yield f"event: {msg['event']}\ndata: {json.dumps(msg['data'])}\n\n"
    finally:
        _subscribers[channel].discard(queue)
