"""Rendered clips: their JSON for the browser, and their finished copies in the video library."""

from __future__ import annotations

import shutil
from pathlib import Path

from ..storage import Storage
from ..tools import atomic_path, slugify
from .media import MediaStore

INFO_KEYS = ("duration_out", "dead_air_removed", "jump_cuts", "shots", "layout_frames", "encoder", "source",
             "loudness_in")


def slim_info(info: dict) -> dict:
    return {k: info.get(k) for k in INFO_KEYS}


class ClipPresenter:
    def __init__(self, media: MediaStore):
        self.media = media

    def present(self, row: dict, current: bool | None = None) -> dict:
        txt = Path(row["txt"]) if row.get("txt") else None
        out = {
            "id": row["id"], "number": row["number"], "version": row["version"], "deleted": bool(row["deleted"]),
            "clip": row["clip"], "overrides": row["overrides"], "info": row["info"],
            "mp4": self.media.url_for(row["chat_id"], row.get("mp4")),
            "jpg": self.media.url_for(row["chat_id"], row.get("jpg")),
            "txt": txt.read_text(encoding="utf-8") if txt and txt.is_file() else "",
        }
        if current is not None:
            out["current"] = current
        return out


class ClipLibrary:
    """The latest version of every web clip, copied to <library>/<video title>/chat<id>_clip_<nn>_<title>.*"""

    def __init__(self, storage: Storage):
        self.storage = storage

    @staticmethod
    def _prefix(chat_id: int, number: int) -> str:
        return f"chat{int(chat_id)}_clip_{int(number):02d}_"

    def _folder(self, chat: dict) -> Path:
        return self.storage.library_dir(chat.get("video_title") or chat.get("title") or "Clippy")

    def publish(self, chat: dict, row: dict) -> Path:
        folder = self._folder(chat)
        self._remove(folder, chat["id"], row["number"])
        stem = self._prefix(chat["id"], row["number"]) + slugify(row["clip"].get("title") or "clip", 40)
        for key in ("mp4", "jpg", "txt"):
            src = Path(row[key]) if row.get(key) else None
            if src and src.is_file():
                with atomic_path(folder / f"{stem}{src.suffix}") as tmp:
                    shutil.copy2(src, tmp)
        return folder / f"{stem}.mp4"

    def unpublish(self, chat: dict, number: int) -> None:
        self._remove(self._folder(chat), chat["id"], number)

    def _remove(self, folder: Path, chat_id: int, number: int) -> None:
        for old in folder.glob(self._prefix(chat_id, number) + "*"):
            old.unlink(missing_ok=True)
