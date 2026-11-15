"""Timelines for clips made before the editor existed: built from the stored clip and the chat's render options."""

from __future__ import annotations

from .operations.placement import place
from .operations.text import HOOK_STYLE, TEXT_WIDTH
from .timeline import HIGHLIGHTS, LAYOUTS, AudioMaster, Camera, CaptionLayer, Cut, TextLayer, TimeRange, Timeline


def timeline_from_clip(clip: dict, options: dict, version: int = 1) -> Timeline:
    """`clip` is a stored Clip dict (start, end, hook…); `options` the render options (layout, cta, highlight…)."""
    o = options or {}
    layers = [CaptionLayer(
        id="captions", font=str(o.get("font") or "Arial Black")[:40],
        highlight=o.get("highlight") if o.get("highlight") in HIGHLIGHTS else "yellow",
        uppercase=bool(o.get("uppercase", True)), language=o.get("captions") if o.get("captions") in ("hu", "en") else None,
    )]
    if o.get("hook", False) and str(clip.get("hook") or "").strip():
        layers.append(TextLayer(id="hook", role="hook", text=str(clip["hook"]).strip()[:120], style=HOOK_STYLE,
                                transform=place("top", TEXT_WIDTH)))
    if str(o.get("cta") or "").strip():
        layers.append(TextLayer(id="cta", role="cta", text=str(o["cta"]).strip()[:120],
                                transform=place("bottom", TEXT_WIDTH)))
    return Timeline(
        version=version,
        cut=Cut(source=TimeRange(start=float(clip["start"]), end=float(clip["end"]))),
        camera=Camera(layout=o.get("layout") if o.get("layout") in LAYOUTS else "auto",
                      punch=bool(o.get("punch", False)), tighten=bool(o.get("tighten", True))),
        layers=tuple(layers),
        audio=AudioMaster(mode="natural" if o.get("audio") == "natural" else "voice",
                          source_music_duck_db=max(0.0, min(15.0, float(o.get("duck") or 0.0)))),
    )
