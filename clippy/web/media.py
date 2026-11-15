"""Which files the browser may load: only a chat's own rendered clips (mp4) and covers (jpg)."""

from __future__ import annotations

import re
from pathlib import Path

from ..storage import Storage

MEDIA_NAME = re.compile(r"^[\w.-]{1,160}\.(mp4|jpg)$")


class MediaStore:
    def __init__(self, storage: Storage):
        self.storage = storage

    def url_for(self, chat_id: int, path: str | None) -> str | None:
        if not path:
            return None
        file = Path(path)
        if file.parent.resolve() != self.storage.chat_clips(chat_id).resolve() or not MEDIA_NAME.match(file.name):
            return None
        return f"/media/{int(chat_id)}/{file.name}"

    def resolve(self, chat_id: int, name: str) -> Path | None:
        if not MEDIA_NAME.match(name):
            return None
        folder = self.storage.chat_clips(chat_id).resolve()
        target = (folder / name).resolve()
        if target.parent != folder or not target.is_file():
            return None
        return target
