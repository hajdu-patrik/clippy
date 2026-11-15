"""Sound: background music from an attachment and the levels of the music and the speech."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

from ...tools import ClippyError
from ..assets import AssetKind
from ..changes import Change
from ..timeline import AudioLayer, Timeline
from ..timeref import Span
from . import Applied, EditContext, Operation, operation

SPEECH = "speech"


@operation
class AddMusic(Operation):
    kind: Literal["add_music"] = "add_music"
    asset: str
    gain_db: float = Field(-16.0, ge=-60, le=6)
    duck: bool = True
    fade_in: float = Field(1.0, ge=0, le=10)
    fade_out: float = Field(2.0, ge=0, le=10)
    span: Span | None = None

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        asset = ctx.assets.require(self.asset, AssetKind.AUDIO, AssetKind.VIDEO)
        layer = AudioLayer(id=ctx.new_id(timeline, "music"), name=asset.name, asset=asset.id, gain_db=self.gain_db,
                           duck=self.duck, fade_in=self.fade_in, fade_out=self.fade_out, span=ctx.span(self.span))
        return timeline.with_layer(layer), [Change.added("music", name=asset.name)]


@operation
class SetSound(Operation):
    """How the speech is processed: "voice" (EQ + compressor) or "natural", and how much the source's own music is
    lowered under the speech (needs voice separation)."""

    kind: Literal["set_sound"] = "set_sound"
    mode: Literal["voice", "natural"] | None = None
    music_duck_db: float | None = Field(None, ge=0, le=15)
    touches_base: ClassVar[bool] = True

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        update = {}
        if self.mode is not None:
            update["mode"] = self.mode
        if self.music_duck_db is not None:
            update["source_music_duck_db"] = self.music_duck_db
        if not update:
            return timeline, []
        return timeline.evolve(audio=timeline.audio.evolve(**update)), [Change.changed("sound")]


@operation
class AdjustAudio(Operation):
    """`layer` is a music layer id or "speech"; unset fields stay as they are."""

    kind: Literal["adjust_audio"] = "adjust_audio"
    layer: str
    gain_db: float | None = Field(None, ge=-60, le=12)
    duck: bool | None = None
    fade_in: float | None = Field(None, ge=0, le=10)
    fade_out: float | None = Field(None, ge=0, le=10)

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        if self.layer == SPEECH:
            if self.gain_db is None:
                return timeline, []
            audio = timeline.audio.evolve(speech_gain_db=max(-20.0, min(12.0, self.gain_db)))
            return timeline.evolve(audio=audio), [Change.changed("speech_volume", db=f"{self.gain_db:+g}")]
        old = timeline.layer(self.layer)
        if old is None or old.kind != "audio":
            raise ClippyError(key="edit.no_layer", layer=self.layer)
        update = {k: getattr(self, k) for k in ("gain_db", "duck", "fade_in", "fade_out") if getattr(self, k) is not None}
        new = old.evolve(**update)
        return timeline.replacing(new), [Change.changed("music", name=new.name or new.asset)]
