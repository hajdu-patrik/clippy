"""Covers with text: the clip's title over its first frame (cover A) and the B title for an A/B test (cover B). The
clean frame is kept beside them, so new titles give new covers."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .tools import atomic_path, log

FONTS = ("ariblk.ttf", "impact.ttf", "arialbd.ttf", "Arial Black.ttf", "Impact.ttf", "Arial Bold.ttf",
         "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "arial.ttf", "DejaVuSans.ttf")
WIDTH_SHARE = 0.86
TOP_SHARE = 0.11
MAX_LINES = 3


def frame_of(cover: Path) -> Path:
    return cover.with_name(f".{cover.stem}.frame.jpg")


def b_cover(cover: Path) -> Path:
    return cover.with_name(f"{cover.stem}.b.jpg")


def _font(fonts: Path | None, size: int):
    for name in FONTS:
        path = (fonts / name) if fonts else None
        if path is not None and path.is_file():
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


def draw_title(frame: Image.Image, text: str, fonts: Path | None) -> Image.Image:
    """Big white words with a black outline in the top part, as large as three lines allow."""
    image = frame.convert("RGB").copy()
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
    for line in lines:
        draw.text((w / 2, y), line, font=font, fill=(255, 255, 255), anchor="ma", stroke_width=stroke,
                  stroke_fill=(0, 0, 0))
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
