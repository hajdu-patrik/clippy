"""Attachments an edit can use: images, audio, video, colour LUTs.

Uploads are never trusted: the type comes from the file's first bytes (not its name), each kind has a size limit,
audio and video must be readable by ffmpeg, images are decoded and re-encoded to PNG under a pixel cap, and LUTs are
parsed strictly. Files live in the chat's folder, so deleting the chat deletes them."""

from __future__ import annotations

import json
import re
import secrets
import shutil
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Iterable

from pydantic import Field

from ..tools import ClippyError, atomic_write_text, probe_media
from .timeline import Frozen

MB = 1024 * 1024
CHUNK = MB
MAX_IMAGE_PIXELS = 40_000_000
MAX_LUT_SIZE = 65


class AssetKind(StrEnum):
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    LUT = "lut"


LIMITS = {AssetKind.IMAGE: 30 * MB, AssetKind.AUDIO: 300 * MB, AssetKind.VIDEO: 2048 * MB, AssetKind.LUT: 8 * MB}


class Asset(Frozen):
    id: str = Field(pattern=r"^[a-z0-9]{6,16}$")
    kind: AssetKind
    name: str = Field(max_length=120)
    path: Path
    duration: float | None = None
    width: int | None = None
    height: int | None = None


class AssetIndex:
    """In-memory lookup over a chat's assets; `require` is the one place that checks an id and its kind."""

    def __init__(self, assets: Iterable[Asset] = ()):
        self.assets = {a.id: a for a in assets}

    def find(self, asset_id: str) -> Asset | None:
        return self.assets.get(asset_id)

    def require(self, asset_id: str, *kinds: AssetKind) -> Asset:
        asset = self.find(asset_id)
        if asset is None:
            raise ClippyError(key="edit.no_asset", asset=asset_id)
        if kinds and asset.kind not in kinds:
            raise ClippyError(key="edit.wrong_asset", asset=asset.name, actual=asset.kind.value,
                              expected=" / ".join(k.value for k in kinds))
        return asset


def sniff(head: bytes) -> tuple[str, str] | None:
    """(family, extension) from the first bytes; "media" means audio or video, decided by probing the streams."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image", "jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image", "webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio", "wav"
    if head.startswith(b"fLaC"):
        return "audio", "flac"
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio", "mp3"
    if head.startswith(b"OggS"):
        return "media", "ogg"
    if head[4:8] == b"ftyp":
        return "media", "m4a" if head[8:11] == b"M4A" else "mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "media", "mkv"
    if re.match(rb"\s*(#|TITLE|LUT_3D_SIZE|DOMAIN_)", head):
        return "lut", "cube"
    return None


def parse_cube(text: str) -> int:
    """The LUT size if the file is a valid 3D .cube, else an error."""
    size, rows = 0, 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(("TITLE", "DOMAIN_")):
            continue
        if line.startswith("LUT_3D_SIZE"):
            size = int(line.split()[1])
            continue
        values = line.split()
        if len(values) != 3 or not all(re.fullmatch(r"-?\d+(\.\d+)?([eE][-+]?\d+)?", v) for v in values):
            raise ValueError("bad LUT row")
        rows += 1
    if not 2 <= size <= MAX_LUT_SIZE or rows != size ** 3:
        raise ValueError("bad LUT size")
    return size


def display_name(name: str) -> str:
    base = re.split(r"[\\/]", name or "")[-1]
    return re.sub(r"[\x00-\x1f\x7f<>:\"|?*]", "", base).strip()[:120] or "file"


class AssetService:
    """Validates, normalises and stores a chat's attachments; metadata is a JSON file next to each asset."""

    def __init__(self, storage):
        self.storage = storage

    def folder(self, chat_id: int) -> Path:
        return self.storage.chat_assets(chat_id)

    def add(self, chat_id: int, name: str, stream: BinaryIO) -> Asset:
        folder = self.folder(chat_id)
        tmp = folder / f".upload-{secrets.token_hex(6)}"
        try:
            self._receive(stream, tmp, max(LIMITS.values()))
            found = sniff(_head(tmp))
            if found is None:
                raise ClippyError(key="asset.unknown", name=display_name(name))
            asset = self._store(chat_id, tmp, display_name(name), *found)
        finally:
            tmp.unlink(missing_ok=True)
        atomic_write_text(folder / f"{asset.id}.json", asset.model_dump_json())
        return asset

    def list(self, chat_id: int) -> list[Asset]:
        out = []
        for meta in sorted(m for m in self.folder(chat_id).glob("*.json") if not m.name.startswith(".")):
            try:
                out.append(Asset.model_validate_json(meta.read_text(encoding="utf-8")))
            except ValueError:
                continue
        return out

    def index(self, chat_id: int) -> AssetIndex:
        return AssetIndex(self.list(chat_id))

    def remove(self, chat_id: int, asset_id: str) -> bool:
        asset = self.index(chat_id).find(asset_id)
        if asset is None:
            return False
        asset.path.unlink(missing_ok=True)
        (self.folder(chat_id) / f"{asset.id}.json").unlink(missing_ok=True)
        return True

    @staticmethod
    def _receive(stream: BinaryIO, target: Path, limit: int) -> None:
        written = 0
        with open(target, "wb") as out:
            while chunk := stream.read(CHUNK):
                written += len(chunk)
                if written > limit:
                    raise ClippyError(key="asset.too_big", limit=limit // MB)
                out.write(chunk)

    def _store(self, chat_id: int, tmp: Path, name: str, family: str, ext: str) -> Asset:
        asset_id = secrets.token_hex(5)
        target = self.folder(chat_id) / asset_id
        if family == "image":
            return self._image(tmp, target.with_suffix(".png"), asset_id, name)
        if family == "lut":
            return self._lut(tmp, target.with_suffix(".cube"), asset_id, name)
        info = probe_media(tmp)
        kind = AssetKind.VIDEO if info["video"] else AssetKind.AUDIO
        if not (info["video"] or info["audio"]) or info["duration"] <= 0 or (family == "audio" and info["video"]):
            raise ClippyError(key="asset.broken", name=name)
        self._check_size(tmp, kind)
        shutil.copyfile(tmp, target.with_suffix(f".{ext}"))
        width, height = info["video"] or (None, None)
        return Asset(id=asset_id, kind=kind, name=name, path=target.with_suffix(f".{ext}"),
                     duration=round(info["duration"], 3), width=width, height=height)

    def _image(self, tmp: Path, target: Path, asset_id: str, name: str) -> Asset:
        import cv2
        import numpy as np

        self._check_size(tmp, AssetKind.IMAGE)
        size = image_dimensions(_head(tmp, 65536))
        if size is None or size[0] * size[1] > MAX_IMAGE_PIXELS:
            raise ClippyError(key="asset.broken", name=name)
        image = cv2.imdecode(np.fromfile(str(tmp), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if image is None:
            raise ClippyError(key="asset.broken", name=name)
        ok, png = cv2.imencode(".png", image)
        if not ok:
            raise ClippyError(key="asset.broken", name=name)
        target.write_bytes(png.tobytes())
        return Asset(id=asset_id, kind=AssetKind.IMAGE, name=name, path=target,
                     width=int(image.shape[1]), height=int(image.shape[0]))

    def _lut(self, tmp: Path, target: Path, asset_id: str, name: str) -> Asset:
        self._check_size(tmp, AssetKind.LUT)
        try:
            text = tmp.read_text(encoding="utf-8")
            parse_cube(text)
        except (UnicodeDecodeError, ValueError, IndexError):
            raise ClippyError(key="asset.broken", name=name) from None
        atomic_write_text(target, text)
        return Asset(id=asset_id, kind=AssetKind.LUT, name=name, path=target)

    @staticmethod
    def _check_size(path: Path, kind: AssetKind) -> None:
        if path.stat().st_size > LIMITS[kind]:
            raise ClippyError(key="asset.too_big", limit=LIMITS[kind] // MB)


def _head(path: Path, size: int = 64) -> bytes:
    with open(path, "rb") as f:
        return f.read(size)


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Width and height from the PNG / JPEG / WebP header, before anything is decoded (decompression bombs)."""
    if data.startswith(b"\x89PNG") and data[12:16] == b"IHDR":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data.startswith(b"\xff\xd8"):
        return _jpeg_dimensions(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X":
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
        if chunk == b"VP8 ":
            return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF
        if chunk == b"VP8L":
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return None


def describe(assets: Iterable[Asset]) -> str:
    """One line per asset for the planner prompt, e.g. 'a1b2c3d4e5 audio summer.mp3 161 s'."""
    lines = []
    for a in assets:
        extra = f" {a.duration:.0f} s" if a.duration else (f" {a.width}x{a.height}" if a.width else "")
        lines.append(f"{a.id} {a.kind.value} {json.dumps(a.name, ensure_ascii=False)}{extra}")
    return "\n".join(lines)
