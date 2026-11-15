"""Tight edit: drop dead air and fillers inside a clip, on the audio envelope and the frame grid."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .audio import HOP, voice_threshold
from .text import ends_sentence, is_filler, merge_suffixes


@dataclass
class EditPlan:
    intervals: list[tuple[float, float]]
    frames: list[tuple[int, int]]
    duration: float
    words: list[dict]
    join_frames: list[int] = field(default_factory=list)
    sentence_frames: list[int] = field(default_factory=list)
    removed: float = 0.0


def _voiced(env, hop, t0, t1, thr):
    i0, i1 = max(0, int(t0 / hop)), min(len(env), int(math.ceil(t1 / hop)))
    if i1 <= i0:
        return np.array([], dtype=int)
    return np.nonzero(env[i0:i1] > thr)[0] + i0


def subtract(ranges: list[list[float]], cuts) -> list[list[float]]:
    """Kept ranges minus the cut ones (both relative to the clip start)."""
    out = []
    for a, b in ranges:
        pieces = [[a, b]]
        for ca, cb in cuts:
            pieces = [p for x, y in pieces for p in ([[x, min(y, ca)], [max(x, cb), y]]) if p[1] - p[0] > 0.04]
        out.extend(pieces)
    return out


def build_edit(words_abs: list[dict], clip_start: float, clip_end: float, fps: float,
               env: np.ndarray | None = None, hop: float = HOP, tighten: bool = True,
               max_gap: float = 0.45, pad_in: float = 0.07, pad_out: float = 0.12,
               exclude: tuple[tuple[float, float], ...] = ()) -> EditPlan:
    """`exclude`: source ranges the user cut out (absolute seconds), removed on top of the tight edit."""
    span = clip_end - clip_start
    rel = [
        {**w, "start": w["start"] - clip_start, "end": w["end"] - clip_start}
        for w in words_abs
        if w["end"] > clip_start + 0.02 and w["start"] < clip_end - 0.02
    ]
    rel = merge_suffixes([w for w in rel if not is_filler(w["word"])])
    thr = voice_threshold(env) if env is not None and len(env) > 10 else None

    groups: list[list[dict]] = []
    for w in rel:
        if groups and tighten:
            prev = groups[-1][-1]
            gap = w["start"] - prev["end"]
            quiet = True
            if thr is not None and gap > 0:
                i0, i1 = int(prev["end"] / hop), int(w["start"] / hop)
                seg = env[max(0, i0): max(0, i1)]
                quiet = len(seg) == 0 or float(np.mean(seg <= thr)) >= 0.7
            if gap > max_gap and quiet:
                groups.append([w])
                continue
        if groups:
            groups[-1].append(w)
        else:
            groups.append([w])

    if not groups:
        k1 = int(round(span * fps))
        return EditPlan([(0.0, k1 / fps)], [(0, k1)], k1 / fps, [], [], [], 0.0)

    raw: list[list[float]] = []
    for gi, g in enumerate(groups):
        first, last = g[0], g[-1]
        prev_end = groups[gi - 1][-1]["end"] if gi else None
        next_start = groups[gi + 1][0]["start"] if gi + 1 < len(groups) else None
        a = 0.0 if gi == 0 else first["start"] - pad_in
        b = span if next_start is None else last["end"] + pad_out
        if thr is not None and gi > 0:
            v = _voiced(env, hop, max(first["start"] - 0.3, prev_end + 0.05), first["start"] + 0.25, thr)
            if len(v):
                a = max(prev_end + 0.03, min(a, v[0] * hop - 0.05))
        if thr is not None and next_start is not None:
            v = _voiced(env, hop, last["end"] - 0.3, min(last["end"] + 0.3, next_start - 0.08), thr)
            if len(v):
                b = max(min(b, v[-1] * hop + 0.09), last["end"] - 0.02)
        raw.append([max(0.0, a), min(span, b)])
    if exclude:
        raw = subtract(raw, [(a - clip_start, b - clip_start) for a, b in exclude])

    frames: list[list[int]] = []
    for a, b in raw:
        ka, kb = int(math.floor(a * fps)), int(math.ceil(b * fps))
        if frames and ka <= frames[-1][1] + 1:
            frames[-1][1] = max(frames[-1][1], kb)
        else:
            frames.append([ka, kb])
    frames_t = [(ka, kb) for ka, kb in frames if kb > ka]
    intervals = [(ka / fps, kb / fps) for ka, kb in frames_t]

    out_words, offset = [], 0.0
    for (a, b) in intervals:
        for w in rel:
            if w["start"] >= a - 0.001 and w["start"] < b:
                out_words.append({**w, "start": round(offset + w["start"] - a, 3),
                                  "end": round(offset + min(w["end"], b) - a, 3)})
        offset += b - a

    sentence_frames, prev = [], None
    for w in rel:
        if prev is None or ends_sentence(prev["word"]):
            sentence_frames.append(int(round(w["start"] * fps)))
        prev = w
    join_frames = [ka for ka, _ in frames_t[1:]]
    return EditPlan(intervals, frames_t, offset, out_words, join_frames, sentence_frames,
                    removed=round(max(0.0, span - offset), 2))
