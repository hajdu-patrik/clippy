"""Sign-in round trips: a single-use state that expires, and PKCE (the verifier never leaves this computer)."""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from typing import Callable

STATE_SECONDS = 600


def new_verifier() -> str:
    return secrets.token_urlsafe(64)[:96]


def s256_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def hex_challenge(verifier: str) -> str:
    """TikTok's desktop sign-in wants the SHA-256 of the verifier as hex instead of base64url."""
    return hashlib.sha256(verifier.encode()).hexdigest()


class OAuthFlows:
    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._pending: dict[str, tuple[str, str, float]] = {}
        self._lock = threading.Lock()

    def start(self, platform: str) -> tuple[str, str]:
        """A new sign-in: (state, verifier)."""
        state, verifier = secrets.token_urlsafe(24), new_verifier()
        with self._lock:
            now = self.clock()
            self._pending = {k: v for k, v in self._pending.items() if now - v[2] < STATE_SECONDS}
            self._pending[state] = (platform, verifier, now)
        return state, verifier

    def finish(self, platform: str, state: str) -> str | None:
        """The verifier of a sign-in this computer started, once; None for an unknown, used or expired state."""
        with self._lock:
            entry = self._pending.pop(state or "", None)
        if entry is None or entry[0] != platform or self.clock() - entry[2] >= STATE_SECONDS:
            return None
        return entry[1]
