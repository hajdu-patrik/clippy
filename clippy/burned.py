"""Captions already burned into a source (a clip someone else captioned, a stream with live captions): bright text
with a dark outline that sits on the same band frame after frame while its words change. A second set of captions on
top of them is noise, so the renderer leaves its own out."""

from __future__ import annotations

import subprocess
from pathlib import Path

import cv2
import numpy as np

from .tools import ffmpeg_path, log, probe_video

SAMPLES = 10
WIDTH = 640
ZONES = 24
BRIGHT, DARK = 200, 50
ROW_SHARE = 0.015
MIN_PRESENCE = 0.5
MAX_OVERLAP = 0.45
TOP_SKIP = 0.08
CENTRED = (0.35, 0.65)


def outlined_text(frame: np.ndarray) -> np.ndarray:
    """Bright (white or yellow) pixels that touch a dark outline, inside the frame's central columns."""
    rgb = frame.astype(np.int16)
    gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
    yellow = (rgb[..., 0] > 180) & (rgb[..., 1] > 170) & (rgb[..., 2] < 120)
    bright = (gray >= BRIGHT) | yellow
    dark = cv2.dilate((gray <= DARK).astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    mask = bright & dark
    w = mask.shape[1]
    mask[:, : w // 20] = False
    mask[:, w - w // 20:] = False
    return mask


def _centre(zone: list[np.ndarray], width: int) -> float:
    """Where the text sits across the frame (0 left, 1 right): captions are centred, a chat box sits at a side."""
    cols = sum(z.sum(axis=0) for z in zone)
    total = float(cols.sum())
    return float((cols * np.arange(width)).sum() / total / width) if total else 0.0


def caption_zone(frames: list[np.ndarray]) -> int | None:
    """The zone (of ZONES horizontal bands) where outlined text shows in most frames with changing content, or None."""
    if len(frames) < 4:
        return None
    masks = [outlined_text(f) for f in frames]
    h, w = masks[0].shape
    edges = np.linspace(0, h, ZONES + 1).astype(int)
    best = None
    for z in range(int(ZONES * TOP_SKIP), ZONES):
        a, b = edges[z], edges[z + 1]
        rows = [m[a:b].sum(axis=1) >= ROW_SHARE * w for m in masks]
        shown = [i for i, r in enumerate(rows) if r.sum() >= 3]
        if len(shown) < MIN_PRESENCE * len(frames):
            continue
        zone = [masks[i][max(0, a - 4): b + 4] for i in shown]
        if not CENTRED[0] <= _centre(zone, w) <= CENTRED[1]:
            continue
        overlaps = []
        for x, y in zip(zone, zone[1:]):
            union = np.logical_or(x, y).sum()
            overlaps.append(np.logical_and(x, y).sum() / union if union else 1.0)
        change = float(np.mean(overlaps)) if overlaps else 1.0
        if change <= MAX_OVERLAP and (best is None or len(shown) > best[1]):
            best = (z, len(shown))
    return None if best is None else best[0]


def _frame(video: Path, at: float, height: int) -> np.ndarray | None:
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-ss", f"{at:.2f}", "-i", str(video),
           "-frames:v", "1", "-vf", f"scale={WIDTH}:{height}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    raw = subprocess.run(cmd, capture_output=True, timeout=60).stdout
    return np.frombuffer(raw, np.uint8).reshape(height, WIDTH, 3) if len(raw) == WIDTH * height * 3 else None


def has_captions(video: Path, start: float, span: float) -> bool:
    meta = probe_video(video)
    height = max(2, round(WIDTH * meta["height"] / meta["width"] / 2) * 2)
    times = [start + span * (i + 0.5) / SAMPLES for i in range(SAMPLES)]
    frames = [f for f in (_frame(video, t, height) for t in times) if f is not None]
    zone = caption_zone(frames)
    if zone is not None:
        log("[render] the video already has captions: no second set on top")
    return zone is not None
