"""Emoji stickers drawn in colour with the system's emoji font (Pillow), saved as PNG for the overlay stage."""

from __future__ import annotations

import importlib.util
import os
from functools import lru_cache
from pathlib import Path

CANDIDATES = (
    Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "seguiemj.ttf",
    Path("/System/Library/Fonts/Apple Color Emoji.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"),
    Path("/usr/share/fonts/noto/NotoColorEmoji.ttf"),
)
NATIVE_SIZES = (109, 160, 137, 96)


@lru_cache(maxsize=1)
def font_path() -> Path | None:
    if importlib.util.find_spec("PIL") is None:
        return None
    return next((p for p in CANDIDATES if p.is_file()), None)


def available() -> bool:
    return font_path() is not None


def _font():
    from PIL import ImageFont

    for size in NATIVE_SIZES:
        try:
            return ImageFont.truetype(str(font_path()), size)
        except OSError:
            continue
    raise OSError("the emoji font cannot be loaded")


def draw(text: str, width: int, path: Path) -> Path:
    """The emoji as a transparent PNG `width` pixels wide (bitmap emoji fonts only come in fixed sizes)."""
    from PIL import Image, ImageDraw

    font = _font()
    left, top, right, bottom = ImageDraw.Draw(Image.new("RGBA", (1, 1))).textbbox((0, 0), text, font=font,
                                                                                   embedded_color=True)
    canvas = Image.new("RGBA", (max(1, right - left) + 8, max(1, bottom - top) + 8), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).text((4 - left, 4 - top), text, font=font, embedded_color=True)
    canvas = canvas.crop(canvas.getbbox() or (0, 0, 1, 1))
    height = max(2, round(canvas.height * width / canvas.width))
    canvas.resize((max(2, width), height), Image.LANCZOS).save(path)
    return path
