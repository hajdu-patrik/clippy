"""Edit operations (Command pattern): small typed models with an `apply`, registered by kind.

The registry yields the JSON schema the LLM must follow and parses its reply, so a new operation is one new class."""

from __future__ import annotations

import importlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Annotated, ClassVar, Union

from pydantic import Field, TypeAdapter, ValidationError

from ..assets import AssetIndex
from ..changes import Change
from ..timeline import Frozen, Timeline
from ..timeref import Clock, Span, TimeResolver


MIN_CLIP_SECONDS = 3.0


@dataclass
class EditContext:
    """What an operation may look at besides the timeline: the attachments, where words fall in the clip, how
    output time maps back to the source (for cuts), this clip's number and the chat's other clips (number: version)."""

    assets: AssetIndex
    resolver: TimeResolver
    clock: Clock
    source_duration: float
    taken: set[str] = field(default_factory=set)
    number: int = 0
    clips: dict[int, int] = field(default_factory=dict)

    def new_id(self, timeline: Timeline, prefix: str) -> str:
        used = {layer.id for layer in timeline.layers} | self.taken
        n = 1 + max((int(m.group(1)) for i in used if (m := re.fullmatch(rf"{prefix}-(\d+)", i))), default=0)
        self.taken.add(f"{prefix}-{n}")
        return f"{prefix}-{n}"

    def span(self, span: Span | None):
        return self.resolver.span(span)

    def describe(self, layer) -> tuple[str, dict]:
        """The change subject and its parameters for a layer, e.g. ("music", {"name": "summer.mp3"})."""
        if layer.kind == "text":
            return (layer.role if layer.role != "custom" else "text"), {"text": layer.text}
        if layer.kind == "sticker":
            return "sticker", {"emoji": layer.emoji}
        if layer.kind == "effect":
            return f"effect_{layer.effect}", {}
        if layer.kind == "sound" and layer.sound:
            return f"sfx_{layer.sound}", {}
        if layer.kind in ("image", "audio", "video", "sound"):
            asset = self.assets.find(layer.asset)
            subject = {"image": "overlay", "audio": "music", "video": "broll", "sound": "sfx"}[layer.kind]
            return subject, {"name": asset.name if asset else layer.asset}
        return layer.kind, {}


Applied = tuple[Timeline, list[Change]]


class Operation(Frozen, ABC):
    kind: str
    touches_base: ClassVar[bool] = False
    control: ClassVar[bool] = False

    @abstractmethod
    def apply(self, timeline: Timeline, ctx: EditContext) -> Applied: ...


REGISTRY: dict[str, type[Operation]] = {}


def operation(cls: type[Operation]) -> type[Operation]:
    REGISTRY[cls.model_fields["kind"].default] = cls
    return cls


class OperationCatalogue:
    """Every registered operation as one discriminated union: schema for the LLM, parser for its reply."""

    def __init__(self, registry: dict[str, type[Operation]] | None = None):
        if registry is None:
            load_all()
        self.registry = dict(registry or REGISTRY)
        union = Union[tuple(self.registry.values())]
        self.adapter = TypeAdapter(list[Annotated[union, Field(discriminator="kind")]])

    def schema(self) -> dict:
        return self.adapter.json_schema()

    def describe(self) -> str:
        """One readable line per operation for the prompt: its fields with types, limits and defaults, then its doc."""
        return "\n".join(f"- {kind}({', '.join(_field(n, f) for n, f in cls.model_fields.items() if n != 'kind')})"
                         + (f": {' '.join(cls.__doc__.split())}" if cls.__doc__ and cls.__doc__ != Operation.__doc__ else "")
                         for kind, cls in sorted(self.registry.items()))

    def parse(self, raw) -> tuple[list[Operation], list[str]]:
        """Valid operations and readable errors (one per invalid item), so one bad item never drops the rest."""
        ops, errors = [], []
        for i, item in enumerate(raw if isinstance(raw, list) else []):
            kind = item.get("kind") if isinstance(item, dict) else None
            if kind not in self.registry:
                errors.append(f"#{i}: unknown kind {kind!r}; use one of: {', '.join(sorted(self.registry))}")
                continue
            try:
                ops.append(self.registry[kind].model_validate(item))
            except ValidationError as exc:
                problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
                errors.append(f"#{i} {kind}: {problems}")
        return ops, errors


def _field(name: str, info) -> str:
    """'gain_db: number -60..6 = -16' style description of one model field."""
    text = re.sub(r"\b(typing|clippy\.editing\.[\w.]+)\.", "", str(info.annotation))
    text = re.sub(r"<class '(\w+)'>", r"\1", text)
    text = re.sub(r"Optional\[(.*)\]", r"\1", text).replace(" | None", "").replace("NoneType", "")
    text = re.sub(r"Literal\[(.*?)\]", lambda m: "|".join(x.strip(" '") for x in m.group(1).split(",")), text)
    for py, word in (("float", "number"), ("int", "integer"), ("str", "text")):
        text = re.sub(rf"\b{py}\b", word, text)
    low = next((getattr(m, k) for m in info.metadata for k in ("ge", "gt") if getattr(m, k, None) is not None), None)
    high = next((getattr(m, k) for m in info.metadata for k in ("le", "lt") if getattr(m, k, None) is not None), None)
    span = f" {low}..{high}" if low is not None and high is not None else ""
    default = "" if info.is_required() else f" = {info.default!r}" if info.default is not None else " (optional)"
    return f"{name}: {text}{span}{default}"

MODULES = ("audio", "meta", "motion", "structure", "text", "visual")


def load_all() -> None:
    """Import the operation modules so their classes register themselves."""
    for name in MODULES:
        importlib.import_module(f"{__name__}.{name}")
