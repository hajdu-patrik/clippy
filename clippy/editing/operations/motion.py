"""Time, frame and motion: speed, aspect ratio, effects, stickers, sound effects, B-roll and joined clips."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ...tools import ClippyError
from ..assets import AssetKind
from ..changes import Change
from ..render import stickers
from ..speed import SpeedMap
from ..timeline import (EFFECTS, FORMATS, SOUNDS, STICKER_MOTIONS, TRANSITIONS, EffectLayer, Join, SoundLayer,
                        SpeedRange, StickerLayer, Timeline, Transition, VideoLayer)
from ..timeref import Span, TimeRef
from . import Applied, EditContext, Operation, operation
from .placement import Position, place

MOMENT_SECONDS = {"zoom": 0.8, "slow_zoom": 3.0, "shake": 0.6, "flash": 0.35, "fade_black": 0.8}
MIN_SPED_SECONDS = 1.0


@operation
class SetSpeed(Operation):
    """factor < 1 slows down (0.5 = half speed, slow motion), > 1 speeds up (2 = double); no span = the whole clip;
    factor 1 makes that part normal speed again."""

    kind: Literal["set_speed"] = "set_speed"
    factor: float = Field(ge=0.25, le=4)
    span: Span | None = None

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        out = ctx.span(self.span)
        if out is None:
            ranges = () if abs(self.factor - 1) < 1e-3 else (SpeedRange(factor=self.factor),)
        else:
            ranges = tuple(r for r in timeline.speed if r.span is None or not r.span.overlaps(out))
            ranges += (SpeedRange(span=out, factor=self.factor),)
        if SpeedMap.of(ranges, ctx.resolver.duration).duration < MIN_SPED_SECONDS:
            raise ClippyError(key="edit.too_short", min=MIN_SPED_SECONDS)
        return timeline.evolve(speed=ranges), [Change.changed("speed", factor=f"{self.factor:g}")]


@operation
class SetFormat(Operation):
    """Aspect ratio of the video: 9:16 (TikTok, Shorts, Reels), 1:1 and 4:5 (feed posts), 16:9 (YouTube, landscape;
    shows the whole source frame)."""

    kind: Literal["set_format"] = "set_format"
    format: Literal[tuple(FORMATS)]

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        return timeline.evolve(format=self.format), [Change.changed("format", format=self.format)]


@operation
class AddEffect(Operation):
    """A visual effect over a span: zoom (a quick punch-in), slow_zoom (a slow push in over the whole span, e.g. on
    a punchline), shake, flash, fade_black (dip to black), black_white, blur, vignette. The first five are moments: a
    span with only a start gets a default length."""

    kind: Literal["add_effect"] = "add_effect"
    effect: Literal[EFFECTS]
    span: Span | None = None
    strength: float = Field(0.6, ge=0.05, le=1)

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        span = self.span
        if self.effect in MOMENT_SECONDS and (span is None or (span.end is None and span.length is None)):
            if span is None or span.start is None:
                raise ClippyError(key="edit.needs_moment")
            span = span.evolve(length=MOMENT_SECONDS[self.effect])
        layer = EffectLayer(id=ctx.new_id(timeline, "effect"), effect=self.effect, strength=self.strength,
                            span=ctx.span(span))
        return timeline.with_layer(layer), [Change.added(f"effect_{self.effect}")]


@operation
class AddSticker(Operation):
    """An emoji sticker in colour (e.g. "🔥", "😂", "👇") at a position; motion: none, fade, pop, bounce, float."""

    kind: Literal["add_sticker"] = "add_sticker"
    emoji: str = Field(min_length=1, max_length=16)
    position: Position = "top-right"
    width: float = Field(0.22, ge=0.05, le=0.6)
    motion: Literal[STICKER_MOTIONS] = "pop"
    span: Span | None = None

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        if not stickers.available():
            raise ClippyError(key="edit.no_emoji_font")
        layer = StickerLayer(id=ctx.new_id(timeline, "sticker"), emoji=self.emoji.strip() or self.emoji,
                             transform=place(self.position, self.width), motion=self.motion, span=ctx.span(self.span))
        return timeline.with_layer(layer), [Change.added("sticker", emoji=layer.emoji)]


@operation
class AddSound(Operation):
    """A sound effect at a moment: a built-in one (pop, ding, boom, click, whoosh, riser, bleep) or an attached
    audio file (asset); exactly one of them."""

    kind: Literal["add_sound"] = "add_sound"
    sound: Literal[SOUNDS] | None = None
    asset: str | None = None
    at: TimeRef
    gain_db: float = Field(-6.0, ge=-40, le=6)

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        if (self.sound is None) == (self.asset is None):
            raise ClippyError(key="edit.one_sound")
        at = round(ctx.resolver.at(self.at), 3)
        if self.asset is not None:
            asset = ctx.assets.require(self.asset, AssetKind.AUDIO, AssetKind.VIDEO)
            layer = SoundLayer(id=ctx.new_id(timeline, "sound"), name=asset.name, asset=asset.id, at=at,
                               gain_db=self.gain_db)
            return timeline.with_layer(layer), [Change.added("sfx", name=asset.name)]
        layer = SoundLayer(id=ctx.new_id(timeline, "sound"), sound=self.sound, at=at, gain_db=self.gain_db)
        return timeline.with_layer(layer), [Change.added(f"sfx_{self.sound}")]


@operation
class AddBroll(Operation):
    """An attached video shown over a span of the clip (B-roll, cutaway): full frame, or small at a position when
    full is false. The speech keeps playing; the video is muted unless gain_db is given."""

    kind: Literal["add_broll"] = "add_broll"
    asset: str
    span: Span | None = None
    full: bool = True
    position: Position = "top"
    width: float = Field(0.6, ge=0.1, le=1)
    gain_db: float | None = Field(None, ge=-40, le=6)

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        asset = ctx.assets.require(self.asset, AssetKind.VIDEO)
        layer = VideoLayer(id=ctx.new_id(timeline, "broll"), name=asset.name, asset=asset.id, full=self.full,
                           transform=place(self.position, self.width), gain_db=self.gain_db, span=ctx.span(self.span))
        return timeline.with_layer(layer), [Change.added("broll", name=asset.name)]


@operation
class JoinClip(Operation):
    """Plays another clip of this chat (its number) after this one, with a transition; joining again moves it last."""

    kind: Literal["join_clip"] = "join_clip"
    clip: int = Field(ge=1)
    transition: Literal[TRANSITIONS] = "fade"
    transition_duration: float = Field(0.5, ge=0.1, le=2.0)

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        if self.clip == ctx.number:
            raise ClippyError(key="edit.self_join")
        version = ctx.clips.get(self.clip)
        if version is None:
            raise ClippyError(key="edit.no_clip", clip=self.clip)
        join = Join(clip=self.clip, version=version,
                    transition=Transition(kind=self.transition, duration=self.transition_duration))
        joins = tuple(j for j in timeline.joins if j.clip != self.clip) + (join,)
        return timeline.evolve(joins=joins), [Change.added("join", clip=self.clip)]


@operation
class UnjoinClip(Operation):
    kind: Literal["unjoin_clip"] = "unjoin_clip"
    clip: int = Field(ge=1)

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        if not any(j.clip == self.clip for j in timeline.joins):
            raise ClippyError(key="edit.no_clip", clip=self.clip)
        joins = tuple(j for j in timeline.joins if j.clip != self.clip)
        return timeline.evolve(joins=joins), [Change.removed("join", clip=self.clip)]
