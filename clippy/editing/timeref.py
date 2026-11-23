"""Where in the clip: users speak in words ("when he says money", "the last 3 seconds"), the renderer needs seconds.

A `Clock` maps the clip's output timeline (after the tight edit) to source seconds; a `TimeResolver` turns time
references into output seconds using the clock and the words the viewer hears."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Literal, Sequence

from pydantic import Field

from ..tools import ClippyError
from .timeline import Frozen, TimeRange
from ..text import fold as plain_fold


class At(Frozen):
    type: Literal["at"] = "at"
    at: float = Field(ge=0)


class Edge(Frozen):
    type: Literal["edge"] = "edge"
    edge: Literal["start", "end"]
    offset: float = 0.0


class Word(Frozen):
    type: Literal["word"] = "word"
    word: str = Field(min_length=1, max_length=40)
    occurrence: int = Field(1, ge=1)


class Phrase(Frozen):
    type: Literal["phrase"] = "phrase"
    phrase: str = Field(min_length=1, max_length=120)


TimeRef = Annotated[At | Edge | Word | Phrase, Field(discriminator="type")]


class Span(Frozen):
    """A start reference plus an end reference or a length; no start means the whole clip."""

    start: TimeRef | None = None
    end: TimeRef | None = None
    length: float | None = Field(None, gt=0)


@dataclass(frozen=True)
class Clock:
    """The kept source intervals, in order; output time runs through them back to back."""

    intervals: tuple[tuple[float, float], ...]

    @property
    def duration(self) -> float:
        return sum(b - a for a, b in self.intervals)

    def to_source(self, t: float) -> float:
        elapsed = 0.0
        for a, b in self.intervals:
            if t <= elapsed + (b - a):
                return a + max(0.0, t - elapsed)
            elapsed += b - a
        return self.intervals[-1][1] if self.intervals else t


def fold(text: str) -> str:
    """A word as it is compared: no accents, case or punctuation."""
    return re.sub(r"[^\w]", "", plain_fold(text))


def corrected(words: Sequence[dict], fixes: Sequence) -> list[dict]:
    """The words with the caption fixes applied: a matched run of words is replaced by the fix's words, which share
    its time in proportion to their length; the run's closing punctuation stays."""
    out = [dict(w) for w in words]
    for fix in fixes:
        target = [t for t in (fold(x) for x in fix.find.split()) if t]
        tokens = fix.replace.split()
        if not target or not tokens:
            continue
        i = 0
        while i <= len(out) - len(target):
            if [fold(w["word"]) for w in out[i:i + len(target)]] != target:
                i += 1
                continue
            run = out[i:i + len(target)]
            tail = re.search(r"[^\w]*$", run[-1]["word"]).group()
            if tail and not re.search(r"[^\w]$", tokens[-1]):
                tokens = [*tokens[:-1], tokens[-1] + tail]
            start, end = run[0]["start"], run[-1]["end"]
            weights = [max(1, len(t)) for t in tokens]
            at, new = start, []
            for token, weight in zip(tokens, weights):
                step = (end - start) * weight / sum(weights)
                new.append({**run[0], "word": token, "start": round(at, 3), "end": round(at + step, 3)})
                at += step
            out[i:i + len(target)] = new
            i += len(new)
    return out


class TimeResolver:
    """Resolves references against the clip as the viewer sees it; words carry output-time starts and ends."""

    def __init__(self, duration: float, words: Sequence[dict]):
        self.duration, self.words = duration, list(words)
        self.folded = [fold(w["word"]) for w in self.words]

    def at(self, ref: TimeRef) -> float:
        if isinstance(ref, At):
            t = ref.at
        elif isinstance(ref, Edge):
            t = (0.0 if ref.edge == "start" else self.duration) + ref.offset
        elif isinstance(ref, Word) and len(ref.word.split()) > 1:
            t = self._phrase(Phrase(phrase=ref.word))
        elif isinstance(ref, Word):
            t = self._word(ref).get("start", 0.0)
        else:
            t = self._phrase(ref)
        if not 0 <= t <= self.duration + 1e-6:
            raise ClippyError(key="edit.outside", at=round(t, 2), duration=round(self.duration, 2))
        return min(max(t, 0.0), self.duration)

    def span(self, span: Span | None) -> TimeRange | None:
        if span is None or (span.start is None and span.end is None and span.length is None):
            return None
        start = self.at(span.start) if span.start is not None else 0.0
        if span.end is not None:
            end = self._end_of(span.end)
        elif span.length is not None:
            end = min(self.duration, start + span.length)
        else:
            end = self.duration
        if end <= start:
            raise ClippyError(key="edit.bad_range", start=round(start, 2), end=round(end, 2))
        return TimeRange(start=round(start, 3), end=round(end, 3))

    def _end_of(self, ref: TimeRef) -> float:
        """A word or a phrase as an end means "until it is said"."""
        if isinstance(ref, Phrase) or (isinstance(ref, Word) and len(ref.word.split()) > 1):
            return self._phrase_end(ref.phrase if isinstance(ref, Phrase) else ref.word)
        return self._word(ref)["end"] if isinstance(ref, Word) else self.at(ref)

    def _word(self, ref: Word) -> dict:
        target, seen = fold(ref.word), 0
        for word, folded in zip(self.words, self.folded):
            if folded == target or (len(target) > 3 and folded.startswith(target)):
                seen += 1
                if seen == ref.occurrence:
                    return word
        raise ClippyError(key="edit.no_word", word=ref.word)

    def _find(self, phrase: str) -> tuple[int, int]:
        target = [fold(w) for w in phrase.split() if fold(w)]
        for i in range(len(self.folded) - len(target) + 1):
            if target and all(self.folded[i + k].startswith(t) for k, t in enumerate(target)):
                return i, i + len(target) - 1
        raise ClippyError(key="edit.no_word", word=phrase)

    def _phrase(self, ref: Phrase) -> float:
        return self.words[self._find(ref.phrase)[0]]["start"]

    def _phrase_end(self, phrase: str) -> float:
        return self.words[self._find(phrase)[1]]["end"]
