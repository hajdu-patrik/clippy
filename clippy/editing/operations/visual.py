"""Pictures on the video and its colours: image / logo overlays, colour adjustments, LUTs."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ..assets import AssetKind
from ..changes import Change
from ..timeline import ColorAdjust, ColorLayer, ImageLayer, Timeline
from ..timeref import Span
from . import Applied, EditContext, Operation, operation
from .placement import Position, place

COLOR_ID = "color"


@operation
class AddOverlay(Operation):
    kind: Literal["add_overlay"] = "add_overlay"
    asset: str
    position: Position = "top-right"
    width: float = Field(0.25, gt=0.02, le=1)
    opacity: float = Field(1.0, ge=0.05, le=1)
    span: Span | None = None

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        asset = ctx.assets.require(self.asset, AssetKind.IMAGE)
        layer = ImageLayer(id=ctx.new_id(timeline, "image"), name=asset.name, asset=asset.id,
                           transform=place(self.position, self.width, self.opacity), span=ctx.span(self.span))
        return timeline.with_layer(layer), [Change.added("overlay", name=asset.name)]


def color_layer(timeline: Timeline) -> ColorLayer:
    return timeline.layer(COLOR_ID) or ColorLayer(id=COLOR_ID)


def with_color(timeline: Timeline, layer: ColorLayer) -> Timeline:
    return timeline.replacing(layer) if timeline.layer(COLOR_ID) else timeline.with_layer(layer)


@operation
class Color(Operation):
    """Absolute values; unset fields keep the current setting (neutral: brightness 0, contrast 1, 6500 K…)."""

    kind: Literal["color"] = "color"
    brightness: float | None = Field(None, ge=-1, le=1)
    contrast: float | None = Field(None, ge=0, le=3)
    saturation: float | None = Field(None, ge=0, le=3)
    gamma: float | None = Field(None, ge=0.1, le=4)
    temperature: int | None = Field(None, ge=1000, le=40000)
    vibrance: float | None = Field(None, ge=-2, le=2)

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        layer = color_layer(timeline)
        changes = {k: getattr(self, k) for k in ColorAdjust.model_fields if getattr(self, k) is not None}
        layer = layer.evolve(adjust=layer.adjust.evolve(**changes))
        return with_color(timeline, layer), [Change.changed("color")]


@operation
class ApplyLut(Operation):
    kind: Literal["apply_lut"] = "apply_lut"
    asset: str
    strength: float = Field(1.0, ge=0, le=1)

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        asset = ctx.assets.require(self.asset, AssetKind.LUT)
        layer = color_layer(timeline).evolve(lut=asset.id, strength=self.strength)
        return with_color(timeline, layer), [Change.added("lut", name=asset.name)]
