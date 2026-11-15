"""Thread-safe pub/sub: worker threads publish, SSE handlers (asyncio) subscribe per chat."""

from __future__ import annotations

import asyncio
import threading


class Hub:
    def __init__(self, queue_size: int = 1000):
        self._subs: dict[int, list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = {}
        self._lock = threading.Lock()
        self._queue_size = queue_size

    def subscribe(self, chat_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        with self._lock:
            self._subs.setdefault(chat_id, []).append((asyncio.get_running_loop(), q))
        return q

    def unsubscribe(self, chat_id: int, q: asyncio.Queue) -> None:
        with self._lock:
            remaining = [(loop, x) for loop, x in self._subs.get(chat_id, []) if x is not q]
            if remaining:
                self._subs[chat_id] = remaining
            else:
                self._subs.pop(chat_id, None)

    def publish(self, chat_id: int | None, event: dict) -> None:
        """chat_id=None broadcasts to every open chat."""
        with self._lock:
            subs = [s for lst in self._subs.values() for s in lst] if chat_id is None else list(self._subs.get(chat_id, []))
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
