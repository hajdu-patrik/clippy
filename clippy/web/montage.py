"""A funny montage: the complete moments a job just made, played one after the other as one clip. The strongest
opens it, the rest build up, and the second strongest closes it; each moment keeps its own ending, and the cuts
between them are quick."""

from __future__ import annotations

from ..editing.operations.motion import JoinClip

CUT = 0.1
MAX_JOINED = 8


def order(rows: list[dict]) -> list[dict]:
    """Strongest first, then the rest from weaker to stronger, the second strongest last."""
    ranked = sorted(rows, key=lambda r: -float((r.get("clip") or {}).get("score") or 0))
    if len(ranked) < 3:
        return ranked
    best, second, rest = ranked[0], ranked[1], ranked[2:]
    return [best, *sorted(rest, key=lambda r: float((r.get("clip") or {}).get("score") or 0)), second]


def joins(rows: list[dict]) -> tuple[dict, list[JoinClip]] | None:
    """The clip the montage is built on and the joins that play the others after it; None for fewer than two."""
    ordered = order(rows)[:MAX_JOINED + 1]
    if len(ordered) < 2:
        return None
    return ordered[0], [JoinClip(clip=r["number"], transition="fade", transition_duration=CUT) for r in ordered[1:]]
