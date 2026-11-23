"""The signed-in accounts: tokens per platform in data/web/accounts.json, refreshed before they run out, and the
developer apps' keys each card was set up with (AppKeys)."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict
from typing import Callable

from ..storage import Storage
from ..tools import ClippyError, write_json
from . import Tokens
from .apps import AppKeys
from .registry import LABELS, PLATFORMS, default_platforms


def new_http():
    import requests

    session = requests.Session()
    session.headers["User-Agent"] = "Clippy"
    return session


class AccountStore:
    def __init__(self, storage: Storage, platforms: dict | None = None, http=None,
                 clock: Callable[[], float] = time.time):
        self.path = storage.accounts_file
        self.platforms = platforms if platforms is not None else default_platforms()
        self.http = http if http is not None else new_http()
        self.clock = clock
        self.apps = AppKeys(storage.apps_file)
        for platform in self.platforms.values():
            platform.keys = self.apps
        self._lock = threading.RLock()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if k in PLATFORMS and isinstance(v, dict)} if isinstance(data, dict) else {}

    def platform(self, name: str):
        if name not in self.platforms:
            raise ClippyError(key="error.unknown_platform", platform=str(name)[:20])
        return self.platforms[name]

    def save(self, name: str, tokens: Tokens) -> None:
        with self._lock:
            data = self._load()
            data[name] = asdict(tokens)
            write_json(self.path, data)

    def remove(self, name: str) -> bool:
        with self._lock:
            data = self._load()
            gone = data.pop(name, None) is not None
            write_json(self.path, data)
            return gone

    def set_up(self, name: str, values: dict) -> None:
        """Keeps the keys typed into a card's setup; only the platform's own fields."""
        platform = self.platform(name)
        self.apps.save(name, values, (*platform.fields, *platform.optional))

    def forget_app(self, name: str) -> None:
        """Drops a card's app keys and the sign-in made with them."""
        self.platform(name)
        self.apps.remove(name)
        self.remove(name)

    def status(self) -> dict:
        """What the browser may see: connected or not, the account's name and when the sign-in runs out."""
        data = self._load()
        out = {}
        for name, platform in self.platforms.items():
            entry = data.get(name) or {}
            out[name] = {"label": LABELS[name], "connected": bool(entry.get("access_token")),
                         "name": str(entry.get("name") or ""), "expires": int(entry.get("expires_at") or 0),
                         "lasting": bool(entry.get("refresh_token")) or platform.renews,
                         "configured": platform.configured(), "oauth": platform.oauth, "posting": platform.posting,
                         "pasteable": platform.pasteable, "console": platform.console, "fields": platform.form(),
                         "saved": bool(self.apps.get(name))}
            if name == "youtube" and entry.get("access_token"):
                out[name]["analytics"] = "yt-analytics" in str(entry.get("scope") or "")
        return out

    def tokens(self, name: str) -> Tokens:
        """Working tokens for a post: refreshed first when they are about to run out."""
        platform = self.platform(name)
        with self._lock:
            entry = self._load().get(name)
            if not entry or not entry.get("access_token"):
                raise ClippyError(key="error.account_missing", platform=LABELS[name])
            tokens = Tokens(**{k: entry[k] for k in Tokens.__dataclass_fields__ if k in entry})
            if platform.needs_refresh(tokens, self.clock()):
                tokens = platform.refresh(self.http, tokens, self.clock())
                self.save(name, tokens)
            return tokens
