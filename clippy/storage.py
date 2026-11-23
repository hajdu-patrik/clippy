"""Where files live: data/cache (shared), data/chats/<id> (per chat), data/work, and the library."""

from __future__ import annotations

import glob
import os
import shutil
import uuid
from pathlib import Path

from .tools import ROOT, safe_name

DATA = ROOT / "data"
DEFAULT_LIBRARY = Path.home() / "Videos" / "Clippy"
CACHE_KINDS = ("downloads", "audio", "transcripts", "picks", "signals")


class Storage:
    def __init__(self, data: Path | str = DATA, library: Path | str | None = None):
        self.data = Path(data).resolve()
        self.library = Path(library or os.environ.get("CLIPPY_LIBRARY") or DEFAULT_LIBRARY).resolve()

    def _dir(self, *parts: str | int) -> Path:
        path = self.data.joinpath(*map(str, parts))
        path.mkdir(parents=True, exist_ok=True)
        return path

    def cache(self, kind: str) -> Path:
        return self._dir("cache", kind)

    @property
    def web(self) -> Path:
        return self._dir("web")

    @property
    def settings_file(self) -> Path:
        return self.web / "settings.json"

    @property
    def llm_file(self) -> Path:
        return self.web / "llm.json"

    @property
    def accounts_file(self) -> Path:
        return self.web / "accounts.json"

    @property
    def apps_file(self) -> Path:
        return self.web / "apps.json"

    def chat_clips(self, chat_id: int) -> Path:
        return self._dir("chats", int(chat_id), "clips")

    def chat_assets(self, chat_id: int) -> Path:
        return self._dir("chats", int(chat_id), "assets")

    def chat_base(self, chat_id: int) -> Path:
        return self._dir("chats", int(chat_id), "base")

    def drop_old_bases(self, chat_ids, now: float, max_age: float) -> int:
        """Editor base renders of chats left alone for `max_age`: removed (the next edit renders its base again)."""
        gone = 0
        for chat_id in chat_ids:
            folder = self.data / "chats" / str(int(chat_id)) / "base"
            files = [f for f in folder.iterdir() if f.is_file()] if folder.is_dir() else []
            if files and now - max(f.stat().st_mtime for f in files) > max_age:
                shutil.rmtree(folder, ignore_errors=True)
                gone += not folder.exists()
        return gone

    def chat_work(self, chat_id: int) -> Path:
        return self._dir("chats", int(chat_id), "work")

    def scratch(self, root: Path | None = None) -> Path:
        path = (root or self._dir("work")) / uuid.uuid4().hex[:12]
        path.mkdir(parents=True)
        return path

    def project(self, title: str) -> Path:
        return self._dir("projects", safe_name(title))

    def library_dir(self, title: str) -> Path:
        path = self.library / safe_name(title)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def purge_chat(self, chat_id: int) -> None:
        shutil.rmtree(self.data / "chats" / str(int(chat_id)), ignore_errors=True)

    def purge_source(self, video_stem: str) -> list[Path]:
        removed = []
        for kind in CACHE_KINDS:
            for file in self.cache(kind).glob(f"{glob.escape(video_stem)}.*"):
                try:
                    file.unlink(missing_ok=True)
                except OSError:
                    continue
                removed.append(file)
        return removed
