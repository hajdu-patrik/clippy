"""The keys of the user's own developer apps, typed in once on an account card: per platform in data/web/apps.json.
They are read only on this computer and never go back to the browser (Settings sees only whether one is set)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from ..tools import write_json

LONGEST = 300


class AppKeys:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, platform: str) -> dict[str, str]:
        entry = self._load().get(platform)
        return {str(k): str(v) for k, v in entry.items()} if isinstance(entry, dict) else {}

    def save(self, platform: str, values: dict[str, str], allowed: tuple[str, ...]) -> None:
        """Stores the typed keys; a field left empty keeps the one saved before."""
        with self._lock:
            data = self._load()
            entry = {k: v for k, v in (data.get(platform) or {}).items() if k in allowed}
            for name in allowed:
                value = "".join(str(values.get(name) or "").split())[:LONGEST]
                if value:
                    entry[name] = value
            data[platform] = entry
            write_json(self.path, data)

    def remove(self, platform: str) -> bool:
        with self._lock:
            data = self._load()
            gone = data.pop(platform, None) is not None
            write_json(self.path, data)
            return gone
