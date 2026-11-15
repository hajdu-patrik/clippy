"""Operations about the edit itself: remove something, ask back, undo."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

from ...tools import ClippyError
from ..changes import Change
from ..timeline import Timeline
from . import Applied, EditContext, Operation, operation

BUMPERS = ("intro", "outro")


@operation
class RemoveLayer(Operation):
    """Removes any layer by id (text, hook, cta, captions, image, music, color) or the intro / outro."""

    kind: Literal["remove_layer"] = "remove_layer"
    layer: str

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        if self.layer in BUMPERS:
            bumper = getattr(timeline, self.layer)
            if bumper is None:
                raise ClippyError(key="edit.no_layer", layer=self.layer)
            asset = ctx.assets.find(bumper.asset)
            return timeline.evolve(**{self.layer: None}), [Change.removed(self.layer, name=asset.name if asset else "")]
        layer = timeline.layer(self.layer)
        if layer is None:
            raise ClippyError(key="edit.no_layer", layer=self.layer)
        subject, params = ctx.describe(layer)
        return timeline.without_layer(self.layer), [Change.removed(subject, **params)]


@operation
class Ask(Operation):
    """Not an edit: the request is ambiguous, so the question goes back to the user."""

    kind: Literal["ask"] = "ask"
    question: str = Field(min_length=1, max_length=300)
    control: ClassVar[bool] = True

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        return timeline, []


@operation
class SetTitle(Operation):
    """Not on the video: the post title shown on the clip card and in the text file (handled by the edit service)."""

    kind: Literal["set_title"] = "set_title"
    title: str = Field(min_length=1, max_length=100)
    control: ClassVar[bool] = True

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        return timeline, []


@operation
class Undo(Operation):
    """Not an edit: the previous version becomes current again (handled by the version history)."""

    kind: Literal["undo"] = "undo"
    control: ClassVar[bool] = True

    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied:
        return timeline, []
