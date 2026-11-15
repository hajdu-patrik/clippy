"""Rendered clips: their JSON for the browser, and their finished copies in the video library."""

from __future__ import annotations

import shutil
from pathlib import Path

from ..covers import b_cover
from ..storage import Storage
from ..tools import atomic_path, slugify
from . import schedule
from .media import MediaStore

INFO_KEYS = ("duration_out", "dead_air_removed", "jump_cuts", "shots", "layout_frames", "encoder", "source",
             "loudness_in")


def slim_info(info: dict) -> dict:
    return {k: info.get(k) for k in INFO_KEYS}


def present_post(post: dict) -> dict:
    problem = post.get("problem") or {}
    return {"platform": post["platform"], "state": post["state"], "url": post["url"], "created": post["created"],
            **({"error_t": problem} if problem.get("key") else {}),
            **({"stats": post["stats"]} if post.get("stats") else {})}


class ClipPresenter:
    def __init__(self, media: MediaStore, db=None):
        self.media, self.db = media, db

    def present(self, row: dict, current: bool | None = None) -> dict:
        txt = Path(row["txt"]) if row.get("txt") else None
        out = {
            "id": row["id"], "number": row["number"], "version": row["version"], "deleted": bool(row["deleted"]),
            "clip": row["clip"], "overrides": row["overrides"], "info": row["info"],
            "mp4": self.media.url_for(row["chat_id"], row.get("mp4")),
            "jpg": self.media.url_for(row["chat_id"], row.get("jpg")),
            "jpg_b": self.media.url_for(row["chat_id"], str(b_cover(Path(row["jpg"])))) if row.get("jpg") and
            b_cover(Path(row["jpg"])).is_file() else None,
            "txt": txt.read_text(encoding="utf-8") if txt and txt.is_file() else "",
            "posts": [present_post(p) for p in self.db.latest_posts(row["chat_id"], row["number"])] if self.db else [],
            "scheduled": [schedule.present(s) for s in self.db.planned(row["chat_id"], row["number"])] if self.db else [],
        }
        if current is not None:
            out["current"] = current
        return out


def rewrite_post_texts(db, library: "ClipLibrary", links: list[str]) -> int:
    """Every current clip's post text again (after the profile links changed), with its library copy."""
    from ..highlights import Clip
    from ..pipeline import post_text
    from ..tools import atomic_write_text

    done = 0
    for chat in db.list_chats():
        for row in db.current_clips(chat["id"]):
            if not row.get("txt") or not Path(row["txt"]).is_file():
                continue
            atomic_write_text(Path(row["txt"]), post_text(Clip.from_dict(row["clip"]), chat.get("source") or "",
                                                          row.get("info") or {}, links))
            library.publish(chat, row)
            done += 1
    return done


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
        second = b_cover(Path(row["jpg"])) if row.get("jpg") else None
        if second is not None and second.is_file():
            with atomic_path(folder / f"{stem}.b.jpg") as tmp:
                shutil.copy2(second, tmp)
        return folder / f"{stem}.mp4"

    def unpublish(self, chat: dict, number: int) -> None:
        self._remove(self._folder(chat), chat["id"], number)

    def _remove(self, folder: Path, chat_id: int, number: int) -> None:
        for old in folder.glob(self._prefix(chat_id, number) + "*"):
            old.unlink(missing_ok=True)

    def remove_chat(self, chat_id: int) -> list[Path]:
        """Every library file a chat made, in any folder (its video title may have changed since); a folder left
        empty goes too. Files the command line made, or other chats, stay."""
        if not self.storage.library.is_dir():
            return []
        removed = []
        for old in self.storage.library.glob(f"*/chat{int(chat_id)}_clip_*"):
            old.unlink(missing_ok=True)
            removed.append(old)
        for folder in {p.parent for p in removed}:
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        return removed
