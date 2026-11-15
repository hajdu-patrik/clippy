"""Where a clip may start and end. A clip starts where a sentence starts and ends where a thought is complete: the
sentence is over and the next one does not carry it on (a connector such as "and" / "de", the answer to a question
it just asked, what a colon announces). Streams and music often come without punctuation, so a clear pause counts
too, a little lower. Nothing here guesses with an LLM; the clip picker's payoff check only moves between these."""

from __future__ import annotations

import re
from typing import Sequence

from .playbook import CONNECTORS
from .text import ends_sentence

PAUSE = 0.7
ANSWER_GAP = 1.5
START_REACH = 3.0
LEAN_BACK = 10.0
END_OVERFLOW = 12.0

COMPLETE_SENTENCE, COMPLETE_PAUSE, OPEN_SENTENCE, OPEN_PAUSE, NONE = 3, 2, 1, 0, -1
_CLOSERS = "\"”»)'"


def start_time(words: Sequence[dict], i: int) -> float:
    """A clip's start just before word i, with a little air that never reaches into the word before."""
    prev_end = words[i - 1]["end"] + 0.04 if i > 0 else 0.0
    return round(max(0.0, min(words[i]["start"], max(words[i]["start"] - 0.2, prev_end))), 3)


def end_time(words: Sequence[dict], i: int) -> float:
    """A clip's end just after word i, with a little air that never reaches into the word after."""
    next_start = words[i + 1]["start"] - 0.06 if i + 1 < len(words) else words[i]["end"] + 0.5
    return round(max(words[i]["end"], min(words[i]["end"] + 0.4, next_start)), 3)


def _plain(word: str) -> str:
    return re.sub(r"[^\wáéíóöőúüű]", "", word.lower())


class Boundaries:
    def __init__(self, words: Sequence[dict]):
        self.words = list(words)
        self.level = [self._end_level(i) for i in range(len(self.words))]
        self.opens = [self._starts(i) for i in range(len(self.words))]

    def _gap_after(self, i: int) -> float:
        return self.words[i + 1]["start"] - self.words[i]["end"] if i + 1 < len(self.words) else 99.0

    def _end_level(self, i: int) -> int:
        word = self.words[i]["word"]
        sentence, pause = ends_sentence(word), self._gap_after(i) >= PAUSE
        if not sentence and not pause:
            return NONE
        if i + 1 >= len(self.words):
            return COMPLETE_SENTENCE if sentence else COMPLETE_PAUSE
        bare = word.rstrip(_CLOSERS)
        carried = (_plain(self.words[i + 1]["word"]) in CONNECTORS or bare.endswith(":")
                   or (bare.endswith("?") and self._gap_after(i) < ANSWER_GAP))
        if sentence:
            return OPEN_SENTENCE if carried else COMPLETE_SENTENCE
        return OPEN_PAUSE if carried else COMPLETE_PAUSE

    def _starts(self, i: int) -> bool:
        return i == 0 or ends_sentence(self.words[i - 1]["word"]) or self._gap_after(i - 1) >= PAUSE

    def duration(self, i0: int, i1: int) -> float:
        return self.words[i1]["end"] - self.words[i0]["start"]

    def start_near(self, t: float, reach: float = START_REACH) -> int | None:
        """The sentence start at or shortly before `t`, else the next one after it. A sentence that opens with a
        connector ("and…", "mert…") leans on the one before it, so that one is taken too when it is short."""
        first = next((i for i, w in enumerate(self.words) if w["start"] >= t - 0.35), None)
        if first is None:
            return None
        back = next((i for i in range(first, -1, -1) if self.opens[i]), None)
        if back is not None and self.words[back]["start"] >= t - reach:
            start = back
        else:
            start = next((i for i in range(first, len(self.words)) if self.opens[i]), first)
        if _plain(self.words[start]["word"]) in CONNECTORS:
            earlier = next((i for i in range(start - 1, -1, -1) if self.opens[i]), None)
            if earlier is not None and self.words[start]["start"] - self.words[earlier]["start"] <= LEAN_BACK:
                return earlier
        return start

    def end_near(self, i0: int, t: float, min_len: float, max_len: float, overflow: float = END_OVERFLOW) -> int | None:
        """The end for a clip from word i0 whose picker wanted it to end near `t`: a complete thought inside the
        length limits (the one nearest `t`), else the first complete one a little past the maximum, else a sentence
        end that the next sentence carries on, else the clearest pause. Never inside a run of words."""
        span = [j for j in range(i0, len(self.words)) if self.duration(i0, j) <= max_len + overflow]
        if not span:
            return None

        def near(level: int, lo: float, hi: float) -> int | None:
            js = [j for j in span if self.level[j] == level and lo <= self.duration(i0, j) <= hi]
            return min(js, key=lambda j: abs(self.words[j]["end"] - t)) if js else None

        def first(level: int, lo: float, hi: float) -> int | None:
            return next((j for j in span if self.level[j] == level and lo < self.duration(i0, j) <= hi), None)

        for found in (near(COMPLETE_SENTENCE, min_len, max_len), near(COMPLETE_PAUSE, min_len, max_len),
                      first(COMPLETE_SENTENCE, max_len, max_len + overflow),
                      first(COMPLETE_PAUSE, max_len, max_len + overflow)):
            if found is not None:
                return found
        loose = [j for j in span if self.level[j] >= OPEN_PAUSE and self.duration(i0, j) >= 0.6 * min_len]
        if loose:
            return min(loose, key=lambda j: (-self.level[j], abs(self.words[j]["end"] - t)))
        tail = [j for j in span if self.duration(i0, j) >= 0.6 * min_len]
        return max(tail, key=self._gap_after) if tail else None

    def complete_ends(self, i0: int, max_dur: float) -> list[int]:
        """Every complete end after word i0 up to a duration, in order (for moving an end by whole sentences)."""
        return [j for j in range(i0, len(self.words)) if self.duration(i0, j) <= max_dur
                and self.level[j] >= COMPLETE_PAUSE]

    def start_at(self, t: float, later: bool) -> float | None:
        """A start time moved to a sentence start: the first at or after `t`, or the last at or before it."""
        opens = [i for i, ok in enumerate(self.opens) if ok]
        i = (next((i for i in opens if self.words[i]["start"] >= t - 0.05), None) if later
             else next((i for i in reversed(opens) if self.words[i]["start"] <= t + 0.05), None))
        return None if i is None else start_time(self.words, i)

    def end_at(self, t: float, later: bool) -> float | None:
        """An end time moved to a complete end: the first at or after `t`, or the last at or before it."""
        ends = ([j for j, level in enumerate(self.level) if level >= COMPLETE_PAUSE]
                or [j for j, level in enumerate(self.level) if level >= OPEN_PAUSE])
        j = (next((j for j in ends if self.words[j]["end"] >= t - 0.05), None) if later
             else next((j for j in reversed(ends) if self.words[j]["end"] <= t + 0.05), None))
        return None if j is None else end_time(self.words, j)

    def sentence_at(self, t: float) -> tuple[int, int] | None:
        """First and last word of the sentence being spoken at `t`; None in a pause or outside the speech."""
        inside = next((i for i, w in enumerate(self.words) if w["start"] <= t <= w["end"]
                       or (i + 1 < len(self.words) and w["end"] < t < self.words[i + 1]["start"]
                           and not self.level[i] >= OPEN_PAUSE)), None)
        if inside is None:
            return None
        first = next(i for i in range(inside, -1, -1) if self.opens[i])
        last = next((j for j in range(inside, len(self.words)) if self.level[j] >= OPEN_PAUSE), len(self.words) - 1)
        return first, last

    def cut_point(self, t: float) -> float:
        """A cut at `t` moved out of the sentence it falls in, to whichever edge of that sentence is nearer, so the
        sentence goes or stays whole; a cut in a pause stays where it is."""
        sentence = self.sentence_at(t)
        if sentence is None:
            return t
        first, last = sentence
        if t - self.words[first]["start"] <= self.words[last]["end"] - t:
            return start_time(self.words, first)
        return self.after(last)

    def after(self, i: int) -> float:
        """Just after word i, before the next word starts."""
        later = self.words[i]["end"] + 0.06
        return round(min(later, self.words[i + 1]["start"]) if i + 1 < len(self.words) else later, 3)

    def index_at_end(self, t: float) -> int | None:
        """The last word that ends by `t`."""
        return next((i for i in range(len(self.words) - 1, -1, -1) if self.words[i]["end"] <= t + 0.05), None)
