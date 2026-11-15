"""Animated word-by-word captions + hook and CTA overlays as an ASS subtitle file (1080x1920)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .playbook import CAPTION_SIDE_MARGIN
from .text import clean_text, ends_clause

PLAY_W, PLAY_H = 1080, 1920


@dataclass
class CaptionStyle:
    font: str = "Arial Black"
    size: int = 84
    highlight: str = "yellow"
    uppercase: bool = True
    max_words: int = 3
    max_chars: int = 18
    margin_v: int = 560
    side_margin: int = CAPTION_SIDE_MARGIN
    hook_seconds: float = 3.0
    cta_seconds: float = 3.0
    lead: float = 0.05

    @property
    def font_name(self) -> str:
        """The font as a safe ASS style field (a comma would shift every column after it)."""
        return re.sub(r"[^\w \-]", "", self.font)[:40].strip() or "Arial Black"


COLORS = {
    "yellow": "&H0000F0FF",
    "green": "&H0000FF5A",
    "cyan": "&H00FFE000",
    "red": "&H003C3CFF",
    "white": "&H00FFFFFF",
}


def _esc(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def _ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _wrap(text: str, width: int = 18) -> str:
    lines, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        lines.append(cur)
    return r"\N".join(lines)


def _overlay(text: str) -> str:
    """Long overlay texts get a smaller font instead of being cut off."""
    wrapped = _wrap(_esc(clean_text(text)))
    n = wrapped.count(r"\N") + 1
    size = "" if n <= 3 else r"\fs66"
    if n > 4:
        wrapped = _wrap(_esc(clean_text(text)), width=24)
        size = r"\fs58"
    return size, wrapped


@dataclass(frozen=True)
class TextEvent:
    """Free text on the frame: centre position in pixels of the frame, colours as #RRGGBB."""

    text: str
    start: float
    end: float
    x: float
    y: float
    size: int = 72
    color: str = "#FFFFFF"
    box: bool = True
    box_color: str = "#000000"
    uppercase: bool = False
    font: str = "Arial Black"
    width: float = 0.85
    animation: str = "fade"


def ass_color(hex_color: str) -> str:
    """#RRGGBB -> ASS &H00BBGGRR&."""
    h = hex_color.lstrip("#")
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}&".upper()


ENTRANCES = {
    "none": "",
    "fade": r"\fad(120,200)",
    "pop": r"\fad(60,150)\fscx40\fscy40\t(0,140,\fscx112\fscy112)\t(140,240,\fscx100\fscy100)",
    "slide": r"\fad(100,150)",
    "typewriter": r"\fad(0,200)",
}
TYPEWRITER_SECONDS = 1.4


def _text_events(e: TextEvent, play_w: int = PLAY_W) -> list[str]:
    chars = max(6, int(e.width * play_w / (e.size * 0.62)))
    body = _wrap(_esc(clean_text(e.text.upper() if e.uppercase else e.text)), width=chars)
    font = CaptionStyle(font=e.font).font_name
    place = (rf"\move({e.x:.0f},{e.y + 160:.0f},{e.x:.0f},{e.y:.0f},0,300)" if e.animation == "slide"
             else rf"\pos({e.x:.0f},{e.y:.0f})")
    tags = (rf"\an5{place}\fn{font}\fs{e.size}\1c{ass_color(e.color)}"
            rf"\3c{ass_color(e.box_color if e.box else '#000000')}{ENTRANCES.get(e.animation, '')}")
    style = "Box" if e.box else "Plain"
    if e.animation != "typewriter":
        return [f"Dialogue: 2,{_ts(e.start)},{_ts(e.end)},{style},,0,0,0,,{{{tags}}}{body}"]
    parts = re.split(r"( |\\N)", body)
    count = (len(parts) + 1) // 2
    step = min(TYPEWRITER_SECONDS, 0.5 * (e.end - e.start)) / max(1, count)
    lines = []
    for i in range(1, count + 1):
        start = e.start + (i - 1) * step
        end = e.start + i * step if i < count else e.end
        lines.append(f"Dialogue: 2,{_ts(start)},{_ts(end)},{style},,0,0,0,,{{{tags}}}{''.join(parts[:2 * i - 1])}")
    return lines


def group_words(words: list[dict], max_words: int, max_chars: int, gap: float = 0.5) -> list[list[dict]]:
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur:
            chars = sum(len(x["word"]) + 1 for x in cur) + len(w["word"])
            prev = cur[-1]
            breaks = (
                len(cur) >= max_words
                or chars > max_chars
                or w["start"] - prev["end"] > gap
                or ends_clause(prev["word"])
            )
            if breaks:
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def build_ass(words: list[dict], clip_start: float, clip_end: float, style: CaptionStyle,
              hook: str = "", cta: str = "", texts: tuple[TextEvent, ...] = (),
              frame: tuple[int, int] = (PLAY_W, PLAY_H)) -> str:
    """words: absolute-time words; output times are relative to clip_start; `frame` is the video's size."""
    hl = COLORS.get(style.highlight, COLORS["yellow"])
    duration = clip_end - clip_start
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {frame[0]}
PlayResY: {frame[1]}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{style.font_name},{style.size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,3,2,{style.side_margin},{style.side_margin},{style.margin_v},1
Style: Hook,{style.font_name},82,&H00FFFFFF,&H00FFFFFF,&H00000000,&H26000000,-1,0,0,0,100,100,0,0,3,22,0,8,60,60,280,1
Style: Cta,{style.font_name},72,&H00FFFFFF,&H00FFFFFF,&H00000000,&H002A2AE0,-1,0,0,0,100,100,0,0,3,22,0,8,60,60,280,1
Style: Box,{style.font_name},72,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,3,22,0,5,0,0,0,1
Style: Plain,{style.font_name},72,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    rel = []
    for w in words:
        if w["end"] <= clip_start or w["start"] >= clip_end:
            continue
        s = max(0.0, w["start"] - clip_start - style.lead)
        if rel:
            s = max(s, rel[-1]["start"] + 0.02)
        rel.append({"start": s, "end": max(s + 0.05, w["end"] - clip_start), "word": w["word"]})
    groups = group_words(rel, style.max_words, style.max_chars)
    for gi, grp in enumerate(groups):
        next_start = groups[gi + 1][0]["start"] if gi + 1 < len(groups) else duration
        last_end = grp[-1]["end"]
        group_end = next_start if next_start - last_end < 0.6 else last_end + 0.3
        group_end = min(group_end, duration)
        group_end = max(group_end, min(next_start, grp[0]["start"] + 0.35))
        tokens = [_esc(w["word"].upper() if style.uppercase else w["word"]) for w in grp]
        for k, w in enumerate(grp):
            start = w["start"] if k else grp[0]["start"]
            end = grp[k + 1]["start"] if k + 1 < len(grp) else group_end
            if end - start < 0.04:
                continue
            parts = []
            for j, tok in enumerate(tokens):
                if j == k:
                    parts.append(rf"{{\c{hl}\fscx108\fscy108\t(0,90,\fscx100\fscy100)}}{tok}{{\r\blur0.6}}")
                else:
                    parts.append(tok)
            events.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Cap,,0,0,0,,{{\\blur0.6}}{' '.join(parts)}")

    if hook:
        hook_end = min(style.hook_seconds, duration)
        fs, txt = _overlay(hook)
        events.append(rf"Dialogue: 1,{_ts(0)},{_ts(hook_end)},Hook,,0,0,0,,{{\fad(120,250){fs}}}{txt}")
    if cta:
        cta_start = max(0.0, duration - style.cta_seconds)
        fs, txt = _overlay(cta)
        events.append(rf"Dialogue: 1,{_ts(cta_start)},{_ts(duration)},Cta,,0,0,0,,{{\fad(200,0){fs}}}{txt}")
    for e in texts:
        events.extend(_text_events(e, frame[0]))
    return header + "\n".join(events) + "\n"


def write_ass(path: Path, *args, **kwargs) -> Path:
    path.write_text(build_ass(*args, **kwargs), encoding="utf-8-sig")
    return path
