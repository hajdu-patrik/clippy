"""The editable state of one clip version. Everything is immutable: an edit returns a new timeline.

Times of layers are seconds on the clip's output timeline (what the viewer sees, after the tight edit); the cut is in
source seconds. The cut and the camera decide the base render, everything else only the finishing pass."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

HEX_COLOR = r"^#[0-9A-Fa-f]{6}$"
HIGHLIGHTS = ("yellow", "green", "cyan", "red", "white")
LAYOUTS = ("auto", "face", "center", "blur", "stream")
TRANSITIONS = ("fade", "fadeblack", "fadewhite", "wipeleft", "wiperight", "slideleft", "slideright", "slideup",
               "slidedown", "circleopen", "circleclose", "zoomin", "dissolve", "smoothleft", "smoothright")
FORMATS = {"9:16": (1080, 1920), "1:1": (1080, 1080), "4:5": (1080, 1350), "16:9": (1920, 1080)}
TEXT_ANIMATIONS = ("none", "fade", "pop", "slide", "typewriter")
STICKER_MOTIONS = ("none", "fade", "pop", "bounce", "float")
EFFECTS = ("zoom", "slow_zoom", "shake", "flash", "fade_black", "black_white", "blur", "vignette")
SOUNDS = ("pop", "ding", "boom", "click", "whoosh", "riser", "bleep")


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    def evolve(self, **changes):
        """A validated copy with some fields changed (model_copy would skip validation)."""
        return type(self).model_validate({**dict(self), **changes})


class TimeRange(Frozen):
    start: float = Field(ge=0)
    end: float = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> "TimeRange":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self

    @property
    def duration(self) -> float:
        return self.end - self.start

    def overlaps(self, other: "TimeRange") -> bool:
        return self.start < other.end and other.start < self.end


class Transform(Frozen):
    """Placement on the 1080×1920 frame: centre point and width as fractions of the frame."""

    x: float = Field(0.5, ge=0, le=1)
    y: float = Field(0.5, ge=0, le=1)
    width: float = Field(0.3, gt=0, le=1)
    opacity: float = Field(1.0, ge=0, le=1)


class TextStyle(Frozen):
    font: str = Field("Arial Black", max_length=40)
    size: int = Field(72, ge=16, le=220)
    color: str = Field("#FFFFFF", pattern=HEX_COLOR)
    box: bool = True
    box_color: str = Field("#000000", pattern=HEX_COLOR)
    uppercase: bool = False
    animation: Literal[TEXT_ANIMATIONS] = "fade"


class ColorAdjust(Frozen):
    brightness: float = Field(0.0, ge=-1, le=1)
    contrast: float = Field(1.0, ge=0, le=3)
    saturation: float = Field(1.0, ge=0, le=3)
    gamma: float = Field(1.0, ge=0.1, le=4)
    temperature: int = Field(6500, ge=1000, le=40000)
    vibrance: float = Field(0.0, ge=-2, le=2)


class BaseLayer(Frozen):
    id: str = Field(pattern=r"^[a-z]+(-\d+)?$")
    name: str = Field("", max_length=80)
    span: TimeRange | None = None


class TextLayer(BaseLayer):
    kind: Literal["text"] = "text"
    role: Literal["custom", "hook", "cta"] = "custom"
    text: str = Field(min_length=1, max_length=200)
    style: TextStyle = TextStyle()
    transform: Transform = Transform(y=0.2, width=0.85)


class WordFix(Frozen):
    """A misheard word or phrase of the transcript and what is really said."""

    find: str = Field(min_length=1, max_length=120)
    replace: str = Field(min_length=1, max_length=120)


class CaptionLayer(BaseLayer):
    kind: Literal["captions"] = "captions"
    font: str = Field("Arial Black", max_length=40)
    size: int = Field(84, ge=24, le=160)
    highlight: Literal[HIGHLIGHTS] = "yellow"
    uppercase: bool = True
    position: float = Field(0.71, ge=0.1, le=0.95)
    visible: bool = True
    language: Literal["hu", "en"] | None = None
    fixes: tuple[WordFix, ...] = Field((), max_length=40)


class ImageLayer(BaseLayer):
    kind: Literal["image"] = "image"
    asset: str
    transform: Transform = Transform()
    fade: float = Field(0.3, ge=0, le=3)


class ColorLayer(BaseLayer):
    kind: Literal["color"] = "color"
    adjust: ColorAdjust = ColorAdjust()
    lut: str | None = None
    strength: float = Field(1.0, ge=0, le=1)


class AudioLayer(BaseLayer):
    kind: Literal["audio"] = "audio"
    asset: str
    gain_db: float = Field(-16.0, ge=-60, le=6)
    duck: bool = True
    fade_in: float = Field(1.0, ge=0, le=10)
    fade_out: float = Field(2.0, ge=0, le=10)


class StickerLayer(BaseLayer):
    """An emoji drawn in colour, placed like an image."""

    kind: Literal["sticker"] = "sticker"
    emoji: str = Field(min_length=1, max_length=16)
    transform: Transform = Transform(width=0.22)
    motion: Literal[STICKER_MOTIONS] = "pop"


class EffectLayer(BaseLayer):
    kind: Literal["effect"] = "effect"
    effect: Literal[EFFECTS]
    strength: float = Field(0.6, ge=0.05, le=1)


class SoundLayer(BaseLayer):
    """A one-shot sound effect starting at `at`: a built-in sound or an attached audio file."""

    kind: Literal["sound"] = "sound"
    sound: Literal[SOUNDS] | None = None
    asset: str | None = None
    at: float = Field(0.0, ge=0)
    gain_db: float = Field(-6.0, ge=-40, le=6)

    @model_validator(mode="after")
    def _one_source(self) -> "SoundLayer":
        if (self.sound is None) == (self.asset is None):
            raise ValueError("give either a built-in sound or an attachment")
        return self


class VideoLayer(BaseLayer):
    """B-roll: an attached video over a span of the clip, full frame or picture-in-picture; muted unless gain is set."""

    kind: Literal["video"] = "video"
    asset: str
    full: bool = True
    transform: Transform = Transform(x=0.5, y=0.3, width=0.6)
    gain_db: float | None = Field(None, ge=-40, le=6)


Layer = Annotated[TextLayer | CaptionLayer | ImageLayer | ColorLayer | AudioLayer | StickerLayer | EffectLayer
                  | SoundLayer | VideoLayer, Field(discriminator="kind")]


class Cut(Frozen):
    """Source seconds: the clip span and the parts removed from it on request (on top of the tight edit)."""

    source: TimeRange
    removed: tuple[TimeRange, ...] = ()


class Camera(Frozen):
    layout: Literal[LAYOUTS] = "auto"
    punch: bool = True
    tighten: bool = True


class Transition(Frozen):
    kind: Literal[TRANSITIONS] = "fade"
    duration: float = Field(0.5, ge=0.1, le=2.0)


class Bumper(Frozen):
    """An intro or outro: an attached video or image shown before / after the clip."""

    asset: str
    transition: Transition = Transition()
    duration: float | None = Field(None, gt=0, le=30)


class Join(Frozen):
    """Another clip of the chat (a stored version) played after this one."""

    clip: int = Field(ge=1)
    version: int = Field(ge=1)
    transition: Transition = Transition()


class SpeedRange(Frozen):
    """Playback speed of a part of the clip (output seconds before any speed change), or of all of it."""

    span: TimeRange | None = None
    factor: float = Field(ge=0.25, le=4)


class AudioMaster(Frozen):
    mode: Literal["voice", "natural"] = "voice"
    speech_gain_db: float = Field(0.0, ge=-20, le=12)
    source_music_duck_db: float = Field(0.0, ge=0, le=15)


class Timeline(Frozen):
    version: int = Field(1, ge=1)
    cut: Cut
    camera: Camera = Camera()
    layers: tuple[Layer, ...] = ()
    intro: Bumper | None = None
    outro: Bumper | None = None
    audio: AudioMaster = AudioMaster()
    format: Literal[tuple(FORMATS)] = "9:16"
    speed: tuple[SpeedRange, ...] = Field((), max_length=12)
    joins: tuple[Join, ...] = Field((), max_length=8)

    @model_validator(mode="after")
    def _unique_ids(self) -> "Timeline":
        ids = [layer.id for layer in self.layers]
        if len(ids) != len(set(ids)):
            raise ValueError("layer ids must be unique")
        return self

    def layer(self, layer_id: str) -> Layer | None:
        return next((layer for layer in self.layers if layer.id == layer_id), None)

    def layers_of(self, kind: str) -> tuple[Layer, ...]:
        return tuple(layer for layer in self.layers if layer.kind == kind)

    @property
    def size(self) -> tuple[int, int]:
        return FORMATS[self.format]

    def text_with_role(self, role: str) -> TextLayer | None:
        return next((layer for layer in self.layers_of("text") if layer.role == role), None)

    def with_layer(self, layer: Layer) -> "Timeline":
        return self.evolve(layers=self.layers + (layer,))

    def without_layer(self, layer_id: str) -> "Timeline":
        return self.evolve(layers=tuple(layer for layer in self.layers if layer.id != layer_id))

    def replacing(self, layer: Layer) -> "Timeline":
        return self.evolve(layers=tuple(layer if old.id == layer.id else old for old in self.layers))

    def next_version(self) -> "Timeline":
        return self.evolve(version=self.version + 1)

    def base_key(self) -> str:
        """Same key, same base render: only the cut and the camera matter."""
        return _digest({"cut": self.cut.model_dump(), "camera": self.camera.model_dump()})

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, text: str) -> "Timeline":
        return cls.model_validate_json(text)


def _digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
