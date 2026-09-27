"""In-process fan-out of change events to SSE subscribers.

One Render instance serves every viewer, so an in-process broadcaster is enough. Running
more than one instance would need Postgres LISTEN/NOTIFY instead (see README.md).
Publishing is thread-safe: sync route handlers run in a worker thread.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any


class Broadcaster:
    def __init__(self, max_queue: int = 256) -> None:
        self._subs: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]] = set()
        self._lock = threading.Lock()
        self._max_queue = max_queue

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(self._max_queue)
        with self._lock:
            self._subs.add((asyncio.get_running_loop(), q))
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subs = {s for s in self._subs if s[1] is not q}

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(_offer, q, event)
            except RuntimeError:  # loop closed: the subscriber is gone
                self.unsubscribe(q)


def _offer(q: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
    try:
        q.put_nowait(event)
    except asyncio.QueueFull:
        # A stalled viewer loses events; tell it to refetch everything instead.
        while not q.empty():
            q.get_nowait()
        q.put_nowait({"kind": "resync"})
