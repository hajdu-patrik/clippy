"""The saved style kit: caption look, a logo and an outro that every new clip gets by itself. The kit's files live in
data/web/kit; a chat that uses them gets its own copies, so changing the kit later never breaks an older clip."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import BinaryIO

from ..editing.assets import Asset, AssetKind, AssetService
from ..editing.operations.structure import SetOutro
from ..editing.operations.visual import AddOverlay
from ..editing.timeline import HIGHLIGHTS
from ..tools import ClippyError, write_json

FONTS = ("Arial Black", "Impact", "Arial")
POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right")
SLOTS = {"logo": (AssetKind.IMAGE,), "outro": (AssetKind.VIDEO, AssetKind.IMAGE)}
DEFAULT = {"font": "", "highlight": "", "uppercase": None, "position": "top-right", "width": 0.22,
           "logo": None, "outro": None}


class KitAssets(AssetService):
    def folder(self, chat_id: int) -> Path:
        path = self.storage.web / "kit"
        path.mkdir(parents=True, exist_ok=True)
        return path


class StyleKit:
    def __init__(self, storage):
        self.storage = storage
        self.files = KitAssets(storage)
        self.path = self.files.folder(0) / "kit.json"

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return {**DEFAULT, **{k: v for k, v in data.items() if k in DEFAULT}} if isinstance(data, dict) else dict(DEFAULT)

    def save(self, font: str = "", highlight: str = "", uppercase: bool | None = None, position: str = "top-right",
             width: float = 0.22) -> dict:
        kit = self.load()
        kit.update(font=font if font in FONTS else "", highlight=highlight if highlight in HIGHLIGHTS else "",
                   uppercase=uppercase if isinstance(uppercase, bool) else None,
                   position=position if position in POSITIONS else "top-right",
                   width=max(0.08, min(0.4, float(width or 0.22))))
        write_json(self.path, kit)
        return kit

    def put(self, slot: str, name: str, stream: BinaryIO) -> dict:
        asset = self.files.add(0, name, stream)
        if asset.kind not in SLOTS[slot]:
            self.files.remove(0, asset.id)
            raise ClippyError(key="error.kit_kind", slot=slot)
        kit = self.load()
        if kit.get(slot):
            self.files.remove(0, kit[slot]["asset"])
        kit[slot] = {"asset": asset.id, "name": asset.name}
        write_json(self.path, kit)
        return kit

    def remove(self, slot: str) -> dict:
        kit = self.load()
        if kit.get(slot):
            self.files.remove(0, kit[slot]["asset"])
            kit[slot] = None
            write_json(self.path, kit)
        return kit

    def render_options(self) -> dict:
        """The caption look for new clips (only what the kit sets)."""
        kit = self.load()
        out = {k: kit[k] for k in ("font", "highlight") if kit.get(k)}
        if isinstance(kit.get("uppercase"), bool):
            out["uppercase"] = kit["uppercase"]
        return out

    def _copy_into(self, chat_id: int, asset_id: str) -> Asset | None:
        asset = self.files.index(0).find(asset_id)
        if asset is None or not asset.path.is_file():
            return None
        folder = self.storage.chat_assets(chat_id)
        target = folder / asset.path.name
        if not target.is_file():
            shutil.copyfile(asset.path, target)
        copy = asset.model_copy(update={"path": target})
        (folder / f"{asset.id}.json").write_text(copy.model_dump_json(), encoding="utf-8")
        return copy

    def operations(self, chat_id: int) -> list:
        """The edits that put the kit's logo and outro on a clip of this chat (none when the kit has neither)."""
        kit, ops = self.load(), []
        if kit.get("logo") and (logo := self._copy_into(chat_id, kit["logo"]["asset"])):
            ops.append(AddOverlay(asset=logo.id, position=kit["position"], width=kit["width"]))
        if kit.get("outro") and (outro := self._copy_into(chat_id, kit["outro"]["asset"])):
            ops.append(SetOutro(asset=outro.id))
        return ops

    def present(self) -> dict:
        kit = self.load()
        return {**kit, "fonts": list(FONTS), "highlights": list(HIGHLIGHTS), "positions": list(POSITIONS)}
