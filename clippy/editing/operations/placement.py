"""Named screen positions the user can say ("top right", "bottom") mapped to transforms on the 9:16 frame."""

from __future__ import annotations

from typing import Literal

from ..timeline import Transform

Position = Literal["top", "center", "bottom", "top-left", "top-right", "bottom-left", "bottom-right"]
POSITIONS: dict[str, tuple[float, float]] = {
    "top": (0.5, 0.2), "center": (0.5, 0.5), "bottom": (0.5, 0.82),
    "top-left": (0.2, 0.09), "top-right": (0.8, 0.09),
    "bottom-left": (0.2, 0.9), "bottom-right": (0.8, 0.9),
}
CAPTION_HEIGHTS = {"high": 0.25, "middle": 0.5, "low": 0.71}


def place(position: str, width: float, opacity: float = 1.0) -> Transform:
    x, y = POSITIONS[position]
    return Transform(x=x, y=y, width=width, opacity=opacity)
