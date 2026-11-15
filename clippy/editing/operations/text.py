"""Text on the video: free text layers, the hook, the call to action and the captions' look."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

from ...tools import ClippyError
from ..changes import Change
from ..timeline import HEX_COLOR, HIGHLIGHTS, CaptionLayer, TextLayer, TextStyle, Timeline, WordFix
from ..timeref import Span, fold
from . import Applied, EditContext, Operation, operation
from .placement import CAPTION_HEIGHTS, Position, place

TEXT_WIDTH = 0.85
HOOK_STYLE = TextStyle(size=82)


class TextLook(Operation):
    """Optional style fields shared by the text operations; unset fields keep the current look."""

    kind: str
    size: int | None = Field(None, ge=16, le=220)
    color: str | None = Field(None, pattern=HEX_COLOR)
    box: bool | None = None
    uppercase: bool | None = None

    def styled(self, style: TextStyle) -> TextStyle:
        changes = {k: getattr(self, k) for k in ("size", "color", "box", "uppercase") if getattr(self, k) is not None}
        return style.evolve(**changes)


def text_layer(timeline: Timeline, layer_id: str) -> TextLayer:
    layer = timeline.layer(layer_id)
    if layer is None or layer.kind != "text":
        raise ClippyError(key="edit.no_layer", layer=layer_id)
    return layer


@operation
class AddText(TextLook):
    kind: Literal["add_text"] = "add_text"
    text: str = Field(min_length=1, max_length=200)
    position: Position = "top"
    span: Span | None = None

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        layer = TextLayer(id=ctx.new_id(timeline, "text"), text=self.text, style=self.styled(TextStyle()),
                          transform=place(self.position, TEXT_WIDTH), span=ctx.span(self.span))
        return timeline.with_layer(layer), [Change.added("text", text=self.text)]


@operation
class EditText(TextLook):
    kind: Literal["edit_text"] = "edit_text"
    layer: str
    text: str | None = Field(None, min_length=1, max_length=200)
    position: Position | None = None
    span: Span | None = None

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        old = text_layer(timeline, self.layer)
        update = {"style": self.styled(old.style)}
        if self.text is not None:
            update["text"] = self.text
        if self.position is not None:
            update["transform"] = place(self.position, old.transform.width, old.transform.opacity)
        if self.span is not None:
            update["span"] = ctx.span(self.span)
        new = old.evolve(**update)
        subject, params = ctx.describe(new)
        return timeline.replacing(new), [Change.changed(subject, **params)]


class RoleText(TextLook):
    """The hook and the CTA are text layers with a role: the renderer times them (first / last seconds)."""

    kind: str
    text: str = Field(min_length=1, max_length=120)
    role: ClassVar[str] = "hook"
    default_position: ClassVar[str] = "top"

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        old = timeline.text_with_role(self.role)
        if old is None:
            base = HOOK_STYLE if self.role == "hook" else TextStyle()
            layer = TextLayer(id=self.role, role=self.role, text=self.text, style=self.styled(base),
                              transform=place(self.default_position, TEXT_WIDTH))
            return timeline.with_layer(layer), [Change.added(self.role, text=self.text)]
        new = old.evolve(text=self.text, style=self.styled(old.style))
        return timeline.replacing(new), [Change.changed(self.role, text=self.text)]


@operation
class SetHook(RoleText):
    kind: Literal["set_hook"] = "set_hook"
    role: ClassVar[str] = "hook"


@operation
class SetCta(RoleText):
    kind: Literal["set_cta"] = "set_cta"
    role: ClassVar[str] = "cta"
    default_position: ClassVar[str] = "bottom"


@operation
class StyleCaptions(Operation):
    kind: Literal["style_captions"] = "style_captions"
    visible: bool | None = None
    font: str | None = Field(None, max_length=40)
    size: int | None = Field(None, ge=24, le=160)
    highlight: Literal[HIGHLIGHTS] | None = None
    uppercase: bool | None = None
    height: Literal["high", "middle", "low"] | None = None
    language: Literal["hu", "en", "spoken"] | None = None

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        old = next(iter(timeline.layers_of("captions")), None)
        update = {k: getattr(self, k) for k in ("visible", "font", "size", "highlight", "uppercase")
                  if getattr(self, k) is not None}
        if self.language is not None:
            update["language"] = None if self.language == "spoken" else self.language
        if self.height is not None:
            update["position"] = CAPTION_HEIGHTS[self.height]
        if old is None:
            layer = CaptionLayer(id="captions", **update)
            return timeline.with_layer(layer), [Change.added("captions")]
        action = Change.removed if update.get("visible") is False else Change.changed
        return timeline.replacing(old.evolve(**update)), [action("captions")]


@operation
class FixCaption(Operation):
    """Correct a misheard word or phrase in the captions (find = as it is written now, replace = what is said)."""

    kind: Literal["fix_caption"] = "fix_caption"
    find: str = Field(min_length=1, max_length=120)
    replace: str = Field(min_length=1, max_length=120)

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        target = " ".join(t for t in (fold(x) for x in self.find.split()) if t)
        heard = " ".join(fold(w["word"]) for w in ctx.resolver.words)
        if not target or f" {target} " not in f" {heard} ":
            raise ClippyError(key="edit.no_word", word=self.find)
        old = next(iter(timeline.layers_of("captions")), None) or CaptionLayer(id="captions")
        fixes = tuple(f for f in old.fixes if " ".join(fold(x) for x in f.find.split()) != target)
        new = old.evolve(fixes=(*fixes, WordFix(find=self.find, replace=self.replace)))
        change = Change.changed("caption_word", find=self.find, replace=self.replace)
        return (timeline.replacing(new) if timeline.layer(old.id) else timeline.with_layer(new)), [change]
