"""Covers with text: the clip's title over a frame of it (cover A) and the B title for an A/B test (cover B). The
clean frame is kept beside them, so new titles give new covers. A stream clip's cover shows the game across the whole
picture with the streamer's camera as a framed card over it."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .compositing import OUT_H, StreamCompositor
from .render import system_fonts
from .text import no_emoji
from .tools import atomic_path, log

FONTS = ("ariblk.ttf", "impact.ttf", "arialbd.ttf", "Arial Black.ttf", "Impact.ttf", "Arial Bold.ttf",
         "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "arial.ttf", "DejaVuSans.ttf")
WIDTH_SHARE = 0.86
TOP_SHARE = 0.11
MAX_LINES = 3
HASHTAG = re.compile(r"\s*#\w+")
ACCENT = (255, 212, 0)
CARD_WIDTH = 0.7
CARD_CENTER = 0.66
CARD_BORDER = 0.012
SEPARATOR_DARK = 12


def frame_of(cover: Path) -> Path:
    return cover.with_name(f".{cover.stem}.frame.jpg")


def b_cover(cover: Path) -> Path:
    return cover.with_name(f"{cover.stem}.b.jpg")


def _font(fonts: Path | None, size: int):
    """The heaviest font found: in Clippy's font folder first, then among this system's caption fonts."""
    for path in [*(fonts / name for name in FONTS if fonts), *system_fonts()]:
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size)


def _wrap(draw, text: str, font, width: float) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and draw.textlength(trial, font=font) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    return lines + ([line] if line else [])


def stream_split(frame: Image.Image) -> int | None:
    """The row where a stream clip's camera ends and its game begins (the black separator line), or None."""
    w, h = frame.size
    row = round(StreamCompositor.TOP_H * h / OUT_H)
    if not 0 < row < h - 2:
        return None
    gray = np.asarray(frame.convert("L"), dtype=np.float32)
    line = gray[max(0, row - 1): row + 2].mean()
    around = max(gray[: max(1, row - 6)].mean(), gray[row + 6:].mean())
    return row if line <= SEPARATOR_DARK and around > line + 8 else None


def _fill(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    w, h = size
    scale = max(w / image.width, h / image.height)
    resized = image.resize((max(w, round(image.width * scale)), max(h, round(image.height * scale))), Image.LANCZOS)
    left, top = (resized.width - w) // 2, (resized.height - h) // 2
    return resized.crop((left, top, left + w, top + h))


def stream_cover(frame: Image.Image, split: int) -> Image.Image:
    """The game fills the picture; the streamer sits on it as a large framed card with a shadow."""
    w, h = frame.size
    image = _fill(frame.crop((0, split + 3, w, h)), (w, h))
    cam = frame.crop((0, 0, w, split - 3))
    card_w = round(w * CARD_WIDTH)
    card = cam.resize((card_w, round(card_w * cam.height / cam.width)), Image.LANCZOS)
    border = max(4, round(w * CARD_BORDER))
    x = (w - card.width) // 2
    y = round(h * CARD_CENTER - card.height / 2)
    box = (x - border, y - border, x + card.width + border, y + card.height + border)
    shadow = Image.new("L", (w, h), 0)
    ImageDraw.Draw(shadow).rounded_rectangle((box[0] + border, box[1] + border * 2, box[2] + border, box[3] + border * 2),
                                             radius=border * 3, fill=170)
    image = Image.composite(Image.new("RGB", (w, h)), image, shadow.filter(ImageFilter.GaussianBlur(border * 2)))
    ImageDraw.Draw(image).rounded_rectangle(box, radius=border * 3, fill=(255, 255, 255))
    mask = Image.new("L", card.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, *card.size), radius=border * 2, fill=255)
    image.paste(card, (x, y), mask)
    return image


def draw_title(frame: Image.Image, text: str, fonts: Path | None) -> Image.Image:
    """Big capitals with a black outline in the top part, as large as three lines allow, the first line white and the
    rest in the accent colour; hashtags stay off. A stream frame becomes the game with the streamer on it first."""
    text = no_emoji(HASHTAG.sub("", text)).upper()
    image = frame.convert("RGB").copy()
    split = stream_split(image)
    if split is not None:
        image = stream_cover(image, split)
    draw = ImageDraw.Draw(image)
    w, h = image.size
    width = WIDTH_SHARE * w
    size = int(w * 0.11)
    while True:
        font = _font(fonts, size)
        lines = _wrap(draw, text, font, width)
        if len(lines) <= MAX_LINES or size <= w * 0.045:
            break
        size = int(size * 0.9)
    lines = lines[:MAX_LINES]
    gap = int(size * 0.18)
    stroke = max(3, size // 12)
    y = int(TOP_SHARE * h)
    shade = Image.new("L", image.size, 0)
    ImageDraw.Draw(shade).rectangle((0, y - size // 2, w, y + len(lines) * (size + gap) + size // 2), fill=110)
    image = Image.composite(Image.new("RGB", image.size, (0, 0, 0)), image, shade)
    draw = ImageDraw.Draw(image)
    for i, line in enumerate(lines):
        draw.text((w / 2, y), line, font=font, fill=(255, 255, 255) if i == 0 else ACCENT, anchor="ma",
                  stroke_width=stroke, stroke_fill=(0, 0, 0))
        y += size + gap
    return image


def make(cover: Path, title: str, alt_title: str = "", fonts: Path | None = None) -> bool:
    """Cover A (title) over the cover image, cover B (B title) beside it; the first call keeps the clean frame."""
    cover = Path(cover)
    frame = frame_of(cover)
    try:
        if not frame.is_file():
            if not cover.is_file():
                return False
            with atomic_path(frame) as tmp:
                tmp.write_bytes(cover.read_bytes())
        with Image.open(frame) as clean:
            clean.load()
            for target, text in ((cover, title), (b_cover(cover), alt_title)):
                if not text.strip():
                    target.unlink(missing_ok=True) if target != cover else None
                    continue
                with atomic_path(target) as tmp:
                    draw_title(clean, text.strip(), fonts).save(tmp, "JPEG", quality=90)
    except OSError as exc:
        log(f"[render] cover text skipped: {exc}")
        return False
    return True
