"""The shape of the clip: what part of the source, what is cut out, the camera, and the intro / outro."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

from ...boundaries import Boundaries, start_time
from ...tools import ClippyError
from ..assets import AssetKind
from ..changes import Change
from ..timeline import LAYOUTS, TRANSITIONS, Bumper, EffectLayer, TimeRange, Timeline, Transition
from ..timeref import Span
from . import MIN_CLIP_SECONDS, Applied, EditContext, Operation, operation

CUT_EFFECT_SECONDS = 0.4
TIMED = ("at", "edge")


def merged(ranges: list[TimeRange]) -> tuple[TimeRange, ...]:
    out: list[TimeRange] = []
    for r in sorted(ranges, key=lambda r: r.start):
        if out and r.start <= out[-1].end:
            out[-1] = TimeRange(start=out[-1].start, end=max(out[-1].end, r.end))
        else:
            out.append(r)
    return tuple(out)


@operation
class CutRange(Operation):
    """Removes a part of the clip as the viewer sees it, e.g. "cut 12–15 s" or "cut from 'but' to 'anyway'";
    transition: what hides the jump (none, flash, fade_black, zoom)."""

    kind: Literal["cut_range"] = "cut_range"
    span: Span
    transition: Literal["none", "flash", "fade_black", "zoom"] = "none"
    touches_base: ClassVar[bool] = True

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        out = ctx.span(self.span)
        if out is None:
            raise ClippyError(key="edit.bad_range", start=0, end=0)
        out = self._whole_sentences(out, ctx)
        if ctx.resolver.duration - out.duration < MIN_CLIP_SECONDS:
            raise ClippyError(key="edit.too_short", min=MIN_CLIP_SECONDS)
        source = TimeRange(start=ctx.clock.to_source(out.start), end=ctx.clock.to_source(out.end))
        cut = timeline.cut.evolve(removed=merged([*timeline.cut.removed, source]))
        timeline = timeline.evolve(cut=cut)
        if self.transition != "none":
            at = max(0.0, out.start - CUT_EFFECT_SECONDS / 2)
            timeline = timeline.with_layer(EffectLayer(
                id=ctx.new_id(timeline, "effect"), effect=self.transition,
                span=TimeRange(start=round(at, 3), end=round(at + CUT_EFFECT_SECONDS, 3))))
        return timeline, [Change.removed("cut", start=round(out.start, 1), end=round(out.end, 1))]

    def _whole_sentences(self, out: TimeRange, ctx: EditContext) -> TimeRange:
        """An edge given in seconds that falls inside a sentence moves to that sentence's nearer edge, so no
        sentence is cut in two; edges given by words are already exact and stay."""
        span, words = self.span, ctx.resolver.words
        if not words:
            return out
        bounds = Boundaries(words)
        start = bounds.cut_point(out.start) if span.start is None or span.start.type in TIMED else out.start
        end = bounds.cut_point(out.end) if span.end is None or span.end.type in TIMED else out.end
        if end <= start:
            sentence = bounds.sentence_at(out.start) or bounds.sentence_at(out.end)
            if sentence is None:
                return out
            first, last = sentence
            start, end = start_time(words, first), bounds.after(last)
        return TimeRange(start=round(min(start, ctx.resolver.duration), 3), end=round(min(end, ctx.resolver.duration), 3))


@operation
class Trim(Operation):
    """Moves the clip's start / end in the source: negative start_shift starts earlier, positive end_shift ends later.
    The new start lands on a sentence start and the new end on a complete thought, in the direction of the move."""

    kind: Literal["trim"] = "trim"
    start_shift: float = Field(0.0, ge=-60, le=60)
    end_shift: float = Field(0.0, ge=-60, le=60)
    touches_base: ClassVar[bool] = True

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        src = timeline.cut.source
        start = max(0.0, src.start + self.start_shift)
        end = min(ctx.source_duration, src.end + self.end_shift)
        if ctx.source_words:
            bounds = Boundaries(ctx.source_words)
            if self.start_shift:
                start = bounds.start_at(start, later=self.start_shift > 0) or start
            if self.end_shift:
                end = min(ctx.source_duration, bounds.end_at(end, later=self.end_shift > 0) or end)
        if end - start < MIN_CLIP_SECONDS:
            raise ClippyError(key="edit.too_short", min=MIN_CLIP_SECONDS)
        span = TimeRange(start=round(start, 3), end=round(end, 3))
        removed = tuple(r for r in timeline.cut.removed if r.overlaps(span))
        cut = timeline.cut.evolve(source=span, removed=removed)
        return timeline.evolve(cut=cut), [Change.changed("trim", start=self.start_shift, end=self.end_shift)]


@operation
class SetCamera(Operation):
    kind: Literal["set_camera"] = "set_camera"
    layout: Literal[LAYOUTS] | None = None
    punch: bool | None = None
    tighten: bool | None = None
    touches_base: ClassVar[bool] = True

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        update = {k: getattr(self, k) for k in ("layout", "punch", "tighten") if getattr(self, k) is not None}
        return timeline.evolve(camera=timeline.camera.evolve(**update)), [Change.changed("camera")]


class SetBumper(Operation):
    """An attached video (or still image, shown for `duration` seconds) before or after the clip."""

    kind: str
    asset: str
    transition: Literal[TRANSITIONS] = "fade"
    transition_duration: float = Field(0.5, ge=0.1, le=2.0)
    duration: float | None = Field(None, gt=0, le=30)
    slot: ClassVar[str] = "intro"

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        asset = ctx.assets.require(self.asset, AssetKind.VIDEO, AssetKind.IMAGE)
        duration = self.duration or (3.0 if asset.kind == AssetKind.IMAGE else None)
        bumper = Bumper(asset=asset.id, duration=duration,
                        transition=Transition(kind=self.transition, duration=self.transition_duration))
        action = Change.changed if getattr(timeline, self.slot) else Change.added
        return timeline.evolve(**{self.slot: bumper}), [action(self.slot, name=asset.name)]


@operation
class SetIntro(SetBumper):
    kind: Literal["set_intro"] = "set_intro"
    slot: ClassVar[str] = "intro"


@operation
class SetOutro(SetBumper):
    kind: Literal["set_outro"] = "set_outro"
    slot: ClassVar[str] = "outro"
