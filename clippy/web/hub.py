"""Thread-safe pub/sub: worker threads publish, SSE handlers (asyncio) subscribe. Every open window gets every event,
tagged with its chat, so the sidebar, the worker bar and the open chat stay live in all windows at once."""

from __future__ import annotations

import asyncio
import threading


class Hub:
    def __init__(self, queue_size: int = 1000):
        self._subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._lock = threading.Lock()
        self._queue_size = queue_size

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        with self._lock:
            self._subs.append((asyncio.get_running_loop(), q))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs = [(loop, x) for loop, x in self._subs if x is not q]

    def publish(self, chat_id: int | None, event: dict) -> None:
        """chat_id=None: an app-wide event (status); otherwise the event belongs to that chat."""
        event = {**event, "chat_id": chat_id}
        with self._lock:
            subs = list(self._subs)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(_offer, q, event)
            except RuntimeError:
                pass


def _offer(q: asyncio.Queue, event: dict) -> None:
    try:
        q.put_nowait(event)
    except asyncio.QueueFull:
        pass
