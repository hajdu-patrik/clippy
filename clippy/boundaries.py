"""Where a clip may start and end. A clip starts where a sentence starts and ends where a thought is complete: the
sentence is over and the next one does not carry it on (a connector such as "and" / "de", the answer to a question
it just asked, what a colon announces). Streams and music often come without punctuation, so a clear pause counts
too, a little lower, unless it is a pause inside a sentence: the word before it cannot end one ("the", "and", "a")
or, in a cased transcript, the words after it go on in lower case. Nothing here guesses with an LLM; the clip
picker's payoff check only moves between these. After the last word the moment may still sound (laughter, a shout,
the reaction to a punchline): `landing` keeps the clip open until it has died down."""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from typing import Sequence

import numpy as np

from .playbook import CONNECTORS
from .text import ends_sentence

PAUSE = 0.7
HELD_PAUSE = 2.0
CASED_SHARE = 0.6
BEAT = 0.5
MAX_LANDING = 2.5
QUIET_DROP_DB = 12.0
QUIET_HOLD = 0.3
SPEECH_WINDOW = 2.0
HANGING = frozenset({
    "the", "a", "an", "to", "of", "in", "on", "at", "for", "with", "from", "by", "into", "about", "my", "your", "his",
    "her", "our", "their", "its", "this", "these", "those", "is", "are", "was", "were", "be", "been", "am", "i", "you",
    "we", "they", "he", "she", "if", "than", "like", "um", "uh", "just", "very", "really", "gonna", "wanna", "kind",
    "az", "egy", "hogy", "ha", "mint", "is", "sem", "csak", "már", "még", "nagyon", "olyan", "ilyen", "ez", "ezt",
    "azt", "én", "te", "ő", "mi", "ti", "ők", "volt", "van", "lesz", "ööö", "öö", "hát",
}) | {w for w in ("and", "but", "or", "so", "because", "és", "de", "mert", "vagy", "meg")}
ANSWER_GAP = 1.5
START_REACH = 3.0
LEAN_BACK = 10.0
END_OVERFLOW = 12.0
SNAP_REACH = 6.0
STRETCHED = 0.8

COMPLETE_SENTENCE, COMPLETE_PAUSE, OPEN_SENTENCE, OPEN_PAUSE, NONE = 3, 2, 1, 0, -1
_CLOSERS = "\"”»)'"


def start_time(words: Sequence[dict], i: int) -> float:
    """A clip's start just before word i, with a little air that never reaches into the word before."""
    prev_end = words[i - 1]["end"] + 0.04 if i > 0 else 0.0
    return round(max(0.0, min(words[i]["start"], max(words[i]["start"] - 0.2, prev_end))), 3)


def end_time(words: Sequence[dict], i: int) -> float:
    """A clip's end just after word i, with a little air that never reaches into the word after. A next word
    stretched over most of a second is a timing that swallowed a silence (its sound comes at its end), so the air
    may go into its start then."""
    if i + 1 >= len(words):
        return round(words[i]["end"] + 0.4, 3)
    after = words[i + 1]
    begins = after["start"]
    if after["end"] - after["start"] > STRETCHED:
        begins = max(begins, after["end"] - 0.4)
    return round(max(words[i]["end"], min(words[i]["end"] + 0.4, begins - 0.06)), 3)


def _plain(word: str) -> str:
    return re.sub(r"[^\wáéíóöőúüű]", "", word.lower())


def _cased(words: Sequence[dict]) -> bool:
    """Whether the transcript writes sentence starts in capitals (then a lower-case word after a pause goes on)."""
    firsts = [words[i + 1]["word"].lstrip("\"“„'(¿¡") for i in range(len(words) - 1) if ends_sentence(words[i]["word"])]
    letters = [w[0] for w in firsts if w and w[0].isalpha()]
    return len(letters) >= 3 and sum(c.isupper() for c in letters) >= CASED_SHARE * len(letters)


def landing(env_db: np.ndarray, hop: float, speech_s: float, room: float) -> float:
    """How long after the last word a clip stays open: at least a beat, and while the sound right after it (laughter,
    a shout, the reaction to the punchline) is still about as loud as the speech before it, until it has stayed quiet
    for a moment, never past `room` (the next words) or MAX_LANDING. `env_db` starts `speech_s` before the last
    word's end."""
    room = max(0.0, room)
    lead = int(round(speech_s / hop))
    if len(env_db) <= lead + 1:
        return min(BEAT, room)
    k = max(1, int(round(0.1 / hop)))
    smooth = np.convolve(env_db, np.ones(k) / k, mode="same")
    speech = smooth[:lead] if lead else smooth[:1]
    quiet = float(np.percentile(speech, 80)) - QUIET_DROP_DB
    tail = smooth[lead:]
    hold = max(1, int(round(QUIET_HOLD / hop)))
    loud = tail >= quiet
    settled = len(tail)
    for j in range(len(tail) - hold + 1):
        if not loud[j:j + hold].any():
            settled = j
            break
    return round(min(room, MAX_LANDING, max(BEAT, settled * hop + 0.2)), 3)


class Boundaries:
    """Every lookup is a binary search over the word times (a long podcast has tens of thousands of words)."""

    def __init__(self, words: Sequence[dict]):
        self.words = list(words)
        self.cased = _cased(self.words)
        self.starts = [w["start"] for w in self.words]
        self.ends = [w["end"] for w in self.words]
        self.level = [self._end_level(i) for i in range(len(self.words))]
        self.opens = [self._starts(i) for i in range(len(self.words))]
        self.open_at = [i for i, ok in enumerate(self.opens) if ok]
        self.end_at_list = ([j for j, level in enumerate(self.level) if level >= COMPLETE_PAUSE]
                            or [j for j, level in enumerate(self.level) if level >= OPEN_PAUSE])

    def _gap_after(self, i: int) -> float:
        return self.words[i + 1]["start"] - self.words[i]["end"] if i + 1 < len(self.words) else 99.0

    def _held(self, i: int) -> bool:
        """A pause inside a sentence: the word before it cannot end one, or (in a cased transcript) the next word goes
        on in lower case without a long silence in between."""
        if _plain(self.words[i]["word"]) in HANGING:
            return True
        if i + 1 >= len(self.words) or not self.cased or self._gap_after(i) >= HELD_PAUSE:
            return False
        nxt = self.words[i + 1]["word"].lstrip("\"“„'(¿¡")
        return bool(nxt) and nxt[0].isalpha() and nxt[0].islower()

    def _end_level(self, i: int) -> int:
        word = self.words[i]["word"]
        sentence, pause = ends_sentence(word), self._gap_after(i) >= PAUSE
        if not sentence and not pause:
            return NONE
        if not sentence and self._held(i):
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
        return i == 0 or ends_sentence(self.words[i - 1]["word"]) or (self._gap_after(i - 1) >= PAUSE
                                                                     and not self._held(i - 1))

    def duration(self, i0: int, i1: int) -> float:
        return self.ends[i1] - self.starts[i0]

    def _upto(self, i0: int, max_dur: float) -> range:
        """Word indices from i0 whose end is within max_dur of word i0's start."""
        return range(i0, bisect_right(self.ends, self.starts[i0] + max_dur))

    def index_at_start(self, t: float) -> int | None:
        """The first word that starts at or after `t`."""
        i = bisect_left(self.starts, t)
        return i if i < len(self.words) else None

    def start_near(self, t: float, reach: float = START_REACH) -> int | None:
        """The sentence start at or shortly before `t`, else the next one after it. A sentence that opens with a
        connector ("and…", "mert…") leans on the one before it, so that one is taken too when it is short."""
        first = self.index_at_start(t - 0.35)
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
        span = self._upto(i0, max_len + overflow)
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
        return [j for j in self._upto(i0, max_dur) if self.level[j] >= COMPLETE_PAUSE]

    @staticmethod
    def _toward(times: list[float], t: float, later: bool, reach: float) -> int | None:
        """Index of the first time at or after `t` (or the last at or before it) no further than `reach` away."""
        k = bisect_left(times, t - 0.05) if later else bisect_right(times, t + 0.05) - 1
        return k if 0 <= k < len(times) and abs(times[k] - t) <= reach else None

    def start_at(self, t: float, later: bool, reach: float = SNAP_REACH) -> float | None:
        """A start time moved to a sentence start: the first at or after `t`, or the last at or before it, within
        `reach`; past that (fast speech without punctuation or pauses) the start of the nearest word that way."""
        k = self._toward([self.starts[i] for i in self.open_at], t, later, reach)
        if k is not None:
            return start_time(self.words, self.open_at[k])
        k = self._toward(self.starts, t, later, reach)
        return start_time(self.words, k) if k is not None else None

    def end_at(self, t: float, later: bool, reach: float = SNAP_REACH) -> float | None:
        """An end time moved to a complete end: the first at or after `t`, or the last at or before it, within
        `reach`; else a sentence end or pause that the next words carry on, else the end of the nearest word. A
        rant without punctuation never sends the end tens of seconds away from where it was asked to be."""
        for found in (self.end_at_list, [j for j, level in enumerate(self.level) if level >= OPEN_PAUSE]):
            k = self._toward([self.ends[j] for j in found], t, later, reach)
            if k is not None:
                return end_time(self.words, found[k])
        k = self._toward(self.ends, t, later, reach)
        return end_time(self.words, k) if k is not None else None

    def sentence_at(self, t: float) -> tuple[int, int] | None:
        """First and last word of the sentence being spoken at `t`; None in a pause or outside the speech."""
        inside = bisect_right(self.starts, t) - 1
        if inside < 0 or (t > self.ends[inside] and (inside + 1 >= len(self.words) or self.level[inside] >= OPEN_PAUSE)):
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
        i = bisect_right(self.ends, t + 0.05) - 1
        return i if i >= 0 else None
