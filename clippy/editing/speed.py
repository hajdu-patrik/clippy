"""Playback speed as a map from the clip's time before the speed change to the time the viewer sees."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .timeline import SpeedRange


@dataclass(frozen=True)
class SpeedMap:
    segments: tuple[tuple[float, float, float], ...]

    @classmethod
    def of(cls, ranges: Sequence[SpeedRange], duration: float) -> "SpeedMap":
        """A whole-clip factor sets the default; a range overrides it inside its span (the later range wins)."""
        base = next((r.factor for r in reversed(ranges) if r.span is None), 1.0)
        parts = [r for r in ranges if r.span is not None]
        points = sorted({0.0, duration, *(min(max(t, 0.0), duration) for r in parts for t in (r.span.start, r.span.end))})
        segments: list[tuple[float, float, float]] = []
        for a, b in zip(points, points[1:]):
            if b - a < 1e-3:
                continue
            mid = (a + b) / 2
            factor = next((r.factor for r in reversed(parts) if r.span.start <= mid < r.span.end), base)
            if segments and abs(segments[-1][2] - factor) < 1e-6:
                segments[-1] = (segments[-1][0], b, factor)
            else:
                segments.append((a, b, factor))
        return cls(tuple(segments) or ((0.0, duration, base),))

    @property
    def identity(self) -> bool:
        return all(abs(f - 1.0) < 1e-6 for _, _, f in self.segments)

    @property
    def duration(self) -> float:
        return sum((b - a) / f for a, b, f in self.segments)

    def out(self, t: float) -> float:
        elapsed = 0.0
        for a, b, f in self.segments:
            if t <= b:
                return elapsed + max(0.0, t - a) / f
            elapsed += (b - a) / f
        return elapsed


def atempo_chain(factor: float) -> list[float]:
    """atempo takes 0.5..100 per instance, so slower speeds are a chain of halvings."""
    steps = []
    while factor < 0.5:
        steps.append(0.5)
        factor /= 0.5
    return [*steps, factor]
