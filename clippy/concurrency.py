"""Shared resources with a fixed lock order, so parallel jobs can wait for each other but never in a circle.

A thread only takes locks of a higher rank than the ones it already holds (checked at runtime); with one global
order a deadlock cannot form. Order: one source video (download, stems, transcript) -> a render slot -> the GPU."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager

_held = threading.local()


class LockOrderError(RuntimeError):
    """A lock was requested out of the global order: a bug that could deadlock, so it fails loudly instead."""


def _stack() -> list[tuple[int, str]]:
    if not hasattr(_held, "stack"):
        _held.stack = []
    return _held.stack


def held() -> tuple[str, ...]:
    return tuple(name for _, name in _stack())


class _Ordered:
    def __init__(self, name: str, rank: int):
        self.name, self.rank = name, rank

    @contextmanager
    def _ordered(self, label: str) -> Iterator[None]:
        stack = _stack()
        if stack and stack[-1][0] >= self.rank:
            raise LockOrderError(f"{label} requested while holding {', '.join(held())}")
        stack.append((self.rank, label))
        try:
            yield
        finally:
            stack.pop()


class Slots(_Ordered):
    """A resource that `slots` jobs may use at the same time (one GPU, two renders…)."""

    def __init__(self, name: str, slots: int, rank: int):
        super().__init__(name, rank)
        self.slots = slots
        self._sem = threading.BoundedSemaphore(slots)

    @contextmanager
    def hold(self) -> Iterator[None]:
        with self._ordered(self.name):
            with self._sem:
                yield


class KeyedLock(_Ordered):
    """One lock per key (a video id): different keys run in parallel, the same key waits its turn."""

    def __init__(self, name: str, rank: int):
        super().__init__(name, rank)
        self._meta = threading.Lock()
        self._locks: dict[str, tuple[threading.Lock, int]] = {}

    @contextmanager
    def hold(self, key: str) -> Iterator[None]:
        with self._ordered(f"{self.name}:{key}"):
            with self._meta:
                lock, users = self._locks.get(key, (threading.Lock(), 0))
                self._locks[key] = (lock, users + 1)
            try:
                with lock:
                    yield
            finally:
                with self._meta:
                    lock, users = self._locks[key]
                    if users <= 1:
                        del self._locks[key]
                    else:
                        self._locks[key] = (lock, users - 1)

    def busy(self, key: str) -> bool:
        with self._meta:
            return key in self._locks


SOURCE = KeyedLock("source", rank=10)
RENDER = Slots("render", int(os.environ.get("CLIPPY_RENDER_SLOTS", "3")), rank=20)
GPU = Slots("gpu", 1, rank=30)


class GuardedLLM:
    """An LLM whose calls take the GPU slot, so the model never loads into VRAM next to Whisper or Demucs."""

    def __init__(self, llm):
        self._llm = llm

    def chat_json(self, *args, **kwargs):
        with GPU.hold():
            return self._llm.chat_json(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._llm, name)

