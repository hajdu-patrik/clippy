"""A small builder for FFmpeg filter graphs. It owns input numbering, pad labels and value escaping, so no compiler
ever concatenates raw filter strings (and nothing a user typed can break out of a filter option)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_OPTION_LEVEL = re.compile(r"([\\':])")
_GRAPH_LEVEL = re.compile(r"([\\'\[\],;])")


def escape(value) -> str:
    """One filter option value, escaped for both of FFmpeg's parsing levels: first the option value (\\ ' :),
    then the filter graph description (\\ ' [ ] , ;)."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return f"{value:.6g}"
    return _GRAPH_LEVEL.sub(r"\\\1", _OPTION_LEVEL.sub(r"\\\1", str(value)))


@dataclass(frozen=True)
class Expr:
    """A trusted FFmpeg expression built by code (never from user text); written as-is but quoted."""

    text: str

    @staticmethod
    def between(start: float, end: float) -> "Expr":
        return Expr(f"between(t,{start:.3f},{end:.3f})")

    def render(self) -> str:
        return "'" + self.text.replace("'", "") + "'"


@dataclass(frozen=True)
class Filter:
    name: str
    args: tuple = ()
    options: tuple = ()

    @classmethod
    def of(cls, name: str, *args, **options) -> "Filter":
        if not re.fullmatch(r"[a-z0-9_]+", name):
            raise ValueError(f"bad filter name: {name}")
        return cls(name, args, tuple((k, v) for k, v in options.items() if v is not None))

    def render(self) -> str:
        parts = [a.render() if isinstance(a, Expr) else escape(a) for a in self.args]
        parts += [f"{k}={v.render() if isinstance(v, Expr) else escape(v)}" for k, v in self.options]
        return self.name + ("=" + ":".join(parts) if parts else "")


@dataclass(frozen=True)
class Pad:
    label: str

    def __str__(self) -> str:
        return f"[{self.label}]"


@dataclass(frozen=True)
class Input:
    index: int
    path: Path
    options: tuple[str, ...]

    @property
    def video(self) -> Pad:
        return Pad(f"{self.index}:v")

    @property
    def audio(self) -> Pad:
        return Pad(f"{self.index}:a")


@dataclass
class FilterGraph:
    inputs: list[Input] = field(default_factory=list)
    chains: list[str] = field(default_factory=list)
    outputs: list[Pad] = field(default_factory=list)
    _count: int = 0

    def input(self, path: Path, *options: str) -> Input:
        item = Input(len(self.inputs), Path(path), tuple(options))
        self.inputs.append(item)
        return item

    def pad(self, prefix: str = "p") -> Pad:
        self._count += 1
        return Pad(f"{prefix}{self._count}")

    def chain(self, source: Pad | list[Pad], *filters: Filter, prefix: str = "p") -> Pad:
        """source → filter → filter → new pad; several sources feed a multi-input filter (overlay, amix, xfade)."""
        if not filters:
            return source if isinstance(source, Pad) else source[0]
        sources = source if isinstance(source, list) else [source]
        out = self.pad(prefix)
        self.chains.append("".join(map(str, sources)) + ",".join(f.render() for f in filters) + str(out))
        return out

    def split(self, source: Pad, n: int, audio: bool = False) -> list[Pad]:
        """One stream into n copies (split / asplit)."""
        outs = [self.pad() for _ in range(n)]
        self.chains.append(f"{source}{'asplit' if audio else 'split'}={n}{''.join(map(str, outs))}")
        return outs

    def discard(self, source: Pad, audio: bool = False) -> None:
        self.chains.append(f"{source}{'anullsink' if audio else 'nullsink'}")

    def output(self, pad: Pad) -> Pad:
        self.outputs.append(pad)
        return pad

    def filter_complex(self) -> str:
        return ";".join(self.chains)

    def input_args(self) -> list[str]:
        args: list[str] = []
        for item in self.inputs:
            args += [*item.options, "-i", str(item.path)]
        return args

    def map_args(self) -> list[str]:
        args: list[str] = []
        for pad in self.outputs:
            args += ["-map", str(pad)]
        return args
