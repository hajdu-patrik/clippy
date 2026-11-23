"""Clip selection: the LLM scores sentence-level transcript windows, then re-ranks and polishes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import i18n, playbook
from .boundaries import END_OVERFLOW, Boundaries, end_time, start_time
from .llm import LLM, think_enabled
from .signals import Signals
from .text import ends_sentence, post_clean
from .tools import ClippyError, fmt_ts, log, report, report_span, write_json

LANG_NAMES = i18n.LANGUAGES

SYSTEM_PROMPT = """You are a senior short-form video editor (10+ years) who turns long podcasts, interviews,
news pieces and streams into viral vertical clips for TikTok, YouTube Shorts and Instagram Reels.

Video title: "{title}"
Spoken language: {language}
On-screen text language (hook, title, hashtags): {text_language}

You receive a transcript window, one sentence per line as [start-end] text (seconds).
Find the moments that would make the strongest STANDALONE clips.

{rubric}
- "start" = the start time of the first sentence, "end" = the end time of the last sentence (copy them exactly).
- Clips must not overlap each other. Prefer fewer, excellent clips. Return at most {per_window} clips.
- Spell names exactly as in the title.

{writing}
- "reason": one sentence (English is fine) why this works.

Return ONLY JSON: {{"clips": [{{"start": <seconds>, "end": <seconds>, "score": <0-100>, "hook": "...",
"hook_type": "...", "alt_hook": "...", "question": "...", "title": "...", "alt_title": "...", "description": "...",
"topic": "...",
"hashtags": ["#..."],
"risks": [], "reason": "..."}}]}}"""

SIGNALS_RULE = """
Lines ending in ⟪…⟫ are where the sound, the picture or the audience reacted: loud = a scream, an explosion or
cheering; laughter; action = a sudden change on screen; chat xN = N times this stream's usual chat activity. In
gameplay and streams these are often the best moments, even when little is said: start a few seconds before the
reaction so the viewer sees what caused it, and end after it lands."""
SPEAKERS_RULE = """
Each line starts with its speaker (A, B…), told apart by their voices: a strong moment is often an exchange
between two of them (a question and its answer, a joke and the laugh, a disagreement)."""
PEAK_REASON = "reaction peak"
LESSONS_RULE = ("\n\nHow this creator's posted clips did so far, against their usual views (a soft preference: a "
                "clearly better moment still wins):\n")
PEAK_LEAD = 0.6

RERANK_PROMPT = """You are the head editor of a viral clips channel. Your team proposed candidate clips from
the video "{title}" (spoken language: {language}). Compare them AGAINST EACH OTHER and pick the best {n}
for TikTok / Shorts / Reels: strongest first-2-second hook, standalone, complete payoff, emotion, quotability.
Avoid picking two clips that tell the same thing.

Prefer clips without risks (misquote, defamation, needs context, spoilers) unless clearly stronger.

You may improve each chosen clip's "hook" (max {max_words} words, native {text_language}, adds curiosity, honestly
paid off; keep it the same hook type) and "title" (max 70 chars, {text_language}, topic keyword early).

Return ONLY JSON: {{"clips": [{{"id": <id>, "score": <0-100>, "hook": "...", "title": "..."}}]}} best first."""


CLIPS_SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"}, "end": {"type": "number"},
                    "score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "hook": {"type": "string"}, "hook_type": {"type": "string", "enum": list(playbook.HOOK_KEYS)},
                    "alt_hook": {"type": "string"}, "question": {"type": "string"}, "title": {"type": "string"},
                    "alt_title": {"type": "string"},
                    "description": {"type": "string"}, "topic": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "risks": {"type": "array", "items": {"type": "string", "enum": list(playbook.RISK_KEYS)}},
                    "reason": {"type": "string"},
                },
                "required": ["start", "end", "score", "hook", "hook_type", "alt_hook", "question", "title",
                             "alt_title", "description", "topic", "hashtags", "risks", "reason"],
            },
        }
    },
    "required": ["clips"],
}
RERANK_SCHEMA = {
    "type": "object",
    "properties": {"clips": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "score": {"type": "integer"}, "hook": {"type": "string"},
                       "title": {"type": "string"}},
        "required": ["id", "score", "hook", "title"]}}},
    "required": ["clips"],
}
POLISH_SCHEMA = {
    "type": "object",
    "properties": {"hooks": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": "integer"}, "hook": {"type": "string"}},
        "required": ["id", "hook"]}}},
    "required": ["hooks"],
}


@dataclass
class Clip:
    start: float
    end: float
    score: float
    hook: str = ""
    title: str = ""
    alt_title: str = ""
    hashtags: list[str] = field(default_factory=list)
    reason: str = ""
    hook_type: str = ""
    alt_hook: str = ""
    question: str = ""
    description: str = ""
    topic: str = ""
    risks: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start

    @classmethod
    def from_dict(cls, d: dict) -> "Clip":
        """Rebuild from to_dict() / clips.json (extra keys such as "duration" are ignored)."""
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duration"] = round(self.duration, 2)
        return d


def sentences(words: list[dict], max_len: float = 20.0) -> list[dict]:
    """Group words into sentences (also split overly long run-ons) -> [{start, end, text}]."""
    out, cur = [], []
    for w in words:
        cur.append(w)
        if ends_sentence(w["word"]) or cur[-1]["end"] - cur[0]["start"] > max_len:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [{"start": c[0]["start"], "end": c[-1]["end"], "text": " ".join(x["word"] for x in c)} for c in out]


def _windows(items: list[dict], window_sec: float, overlap_sec: float) -> list[list[dict]]:
    if not items:
        return []
    total = items[-1]["end"]
    out, start = [], items[0]["start"]
    while start < total:
        end = start + window_sec
        win = [s for s in items if s["start"] >= start - 0.01 and s["start"] < end]
        if win:
            out.append(win)
        if end >= total:
            break
        start = end - overlap_sec
    return out


def _format_window(win: list[dict], signals: Signals | None = None) -> str:
    def line(s: dict) -> str:
        tags = signals.tags(s["start"], s["end"]) if signals else ""
        who = f"{s['speaker']}: " if s.get("speaker") else ""
        return f"[{s['start']:.2f}-{s['end']:.2f}] {who}{s['text']}" + (f"  ⟪{tags}⟫" if tags else "")
    return "\n".join(line(s) for s in win)


def _speaks(c: Clip, words: list[dict]) -> bool:
    return any(c.start - 0.05 <= w["start"] <= c.end for w in words)


def _peak_clips(signals: Signals, covered: list[Clip], min_len: float, max_len: float, duration: float,
                limit: int) -> list[Clip]:
    """Candidates around the strongest reactions that no transcript window proposed: the build-up first, then the
    reaction and a little after it."""
    length = max(min_len, min(max_len, 0.5 * (min_len + max_len)))
    out = []
    for peak in signals.peaks(limit):
        if any(c.start <= peak.time <= c.end for c in covered + out):
            continue
        start = max(0.0, peak.time - PEAK_LEAD * length)
        end = min(duration or peak.time + length, start + length)
        start = max(0.0, min(start, end - length))
        if end - start >= min_len * 0.6:
            out.append(Clip(start, end, round(45 + 45 * peak.strength, 1),
                            reason=f"{PEAK_REASON}: {signals.describe(start, end) or ', '.join(peak.kinds)}"))
    return out


def _snap(clip: Clip, words: list[dict], min_len: float, max_len: float,
          bounds: Boundaries | None = None) -> Clip | None:
    """Start the clip where a sentence starts and end it where the thought is complete (see boundaries), as near
    as possible to the picker's choice; a complete ending is worth a little more than the length limit."""
    if not words:
        return None
    bounds = bounds or Boundaries(words)
    i0 = bounds.start_near(clip.start)
    i1 = None if i0 is None else bounds.end_near(i0, clip.end, min_len, max_len)
    if i1 is None or bounds.duration(i0, i1) < min_len * 0.6:
        return None
    return _place(clip, words, i0, i1)


def _place(clip: Clip, words: list[dict], i0: int, i1: int) -> Clip:
    """Clip times from its first and last word."""
    clip.start, clip.end = start_time(words, i0), end_time(words, i1)
    return clip


def _overlap(a: Clip, b: Clip) -> float:
    inter = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    return inter / max(1e-6, min(a.duration, b.duration))


def _heuristic(sents: list[dict], min_len: float, max_len: float, signals: Signals | None = None) -> list[Clip]:
    """No-LLM fallback: favour lively stretches (exclamations, questions, fast speech)."""
    target = (min_len + max_len) / 2
    cands = []
    for i, s in enumerate(sents):
        j, text = i, s["text"]
        while j + 1 < len(sents) and sents[j]["end"] - s["start"] < target:
            j += 1
            text += " " + sents[j]["text"]
        span = sents[j]["end"] - s["start"]
        if span < min_len:
            continue
        words = len(text.split())
        score = 40 + 6 * text.count("!") + 4 * text.count("?") + 8 * (words / span - 2.0)
        if signals:
            score += 8 * len(signals.kinds(s["start"], sents[j]["end"]))
        cands.append(Clip(s["start"], sents[j]["end"], round(min(95, max(5, score)), 1),
                          hook=" ".join(s["text"].split()[:7]), title=s["text"][:70],
                          reason="heuristic: lively speech"))
    return cands


def _clip_text(c: Clip, words: list[dict], limit: int = 700) -> str:
    text = " ".join(w["word"] for w in words if c.start - 0.05 <= w["start"] <= c.end)
    return text[:limit] + ("…" if len(text) > limit else "")


def _rerank(llm: LLM, cands: list[Clip], words: list[dict], n: int, title: str, language: str,
            text_language: str, signals: Signals | None = None, lessons: str = "") -> list[Clip]:
    def reactions(c: Clip) -> str:
        found = signals.describe(c.start, c.end) if signals else ""
        return f" | reactions: {found}" if found else ""

    lines = [
        f"id={i} | {c.duration:.0f}s | first score {c.score:.0f} | hook: {c.hook}"
        f"{' | risks: ' + ', '.join(c.risks) if c.risks else ''}{reactions(c)}\n"
        f"{_clip_text(c, words) or '(no speech)'}"
        for i, c in enumerate(cands)
    ]
    try:
        prompt = RERANK_PROMPT.format(title=title, language=language, text_language=text_language, n=n,
                                      max_words=playbook.HOOK_MAX_WORDS) + (LESSONS_RULE + lessons if lessons else "")
        reply = llm.chat_json(prompt,
                              "\n\n".join(lines),
                              think=think_enabled(), schema=RERANK_SCHEMA)
    except ClippyError as exc:
        log(f"[hook] re-ranking skipped: {exc}")
        return cands
    picked: list[Clip] = []
    for item in reply.get("clips", []) if isinstance(reply, dict) else []:
        try:
            c = cands[int(item["id"])]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        if c in picked:
            continue
        c.score = float(item.get("score", c.score))
        c.hook = playbook.tidy_hook(str(item.get("hook") or c.hook))
        c.title = str(item.get("title") or c.title).strip()
        picked.append(c)
    if not picked:
        return cands
    rest = [c for c in cands if c not in picked]
    return picked + sorted(rest, key=lambda c: c.score, reverse=True)


ENDINGS_PROMPT = """You check where short video clips END. For each clip you get its last sentences and, after
"NEXT:", what is said right after it in the video ({language}).
A good ending is where the clip's topic is finished: the punchline, the conclusion, the answer, the end of the
story or the rant. A bad ending stops while the same topic, story, argument or rant still goes on, on a set-up, a
list that goes on, a promise or a question that the next sentences answer, or it adds the first sentence of a new
topic. It is also a bad ending when the speaker only paused and NEXT carries the same sentence or thought on, or
when NEXT is the quick reaction to the clip's last line (a laugh, a comeback, "no way", the answer to its question):
that reaction belongs to the moment. The length does not matter here: never end a topic early to keep a clip short.
When NEXT says the video
ends and the clip's last words are an unfinished sentence (the recording was cut off mid-sentence), that is a bad
ending too: answer "trim" with "words": how many of the clip's last words belong to that unfinished sentence.
For each clip answer:
- "ok": the topic is finished;
- "extend" with "sentences" 1 to {max_steps}: the next sentence(s) still belong to this topic and finish it;
- "trim": the clip's last sentence already starts something new that the clip does not need.
Return ONLY JSON: {{"clips": [{{"id": <id>, "verdict": "ok" | "extend" | "trim", "sentences": 1, "words": 0}}]}}"""

STARTS_PROMPT = """You check where short video clips START. For each clip you get the sentences said right BEFORE
it, numbered backwards from the clip ([-1] is the one just before it), then its first sentences after "CLIP:"
({language}).
A good start opens the clip's topic: a new viewer knows what is being talked about. A bad start drops into the
middle of a story, a rant, an argument or an answer whose beginning is in the sentences before, or it opens on
filler or on the end of the previous topic.
For each clip answer:
- "ok": the clip starts where its topic starts;
- "extend" with "sentences" 1 to {max_steps}: the topic starts that many sentences earlier ([-1] = 1);
- "trim" with "sentences" 1 or 2: the clip's first sentence(s) are filler or still the previous topic.
Return ONLY JSON: {{"clips": [{{"id": <id>, "verdict": "ok" | "extend" | "trim", "sentences": 1}}]}}"""
MAX_STEPS, CHECK_ROUNDS, HEAD_WORDS, MAX_CUT_WORDS = 4, 2, 40, 15
OVERLAP = 0.15

EDGES_SCHEMA = {
    "type": "object",
    "properties": {"clips": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": "integer"},
                                         "verdict": {"type": "string", "enum": ["ok", "extend", "trim"]},
                                         "sentences": {"type": "integer"}, "words": {"type": "integer"}},
        "required": ["id", "verdict"]}}},
    "required": ["clips"],
}
TAIL_WORDS, NEXT_WORDS = 45, 40


def _words_text(words: list[dict]) -> str:
    return " ".join(w["word"] for w in words)


def _ask_windows(llm: LLM, system: str, wins: list[list[dict]], cache: PickCache | None,
                 signals: Signals | None = None) -> tuple[list[Clip], bool]:
    """Every window's candidates, and whether all of them came from the cache."""
    raw, all_cached = [], True
    for n, win in enumerate(wins, 1):
        report(0.8 * (n - 1) / len(wins))
        log(f"[hook] window {n}/{len(wins)}  ({fmt_ts(win[0]['start'])}-{fmt_ts(win[-1]['end'])})  {llm.describe()}")
        text = "TRANSCRIPT:\n" + _format_window(win, signals)
        if cache is None:
            reply, hit = llm.chat_json(system, text, think=think_enabled(), schema=CLIPS_SCHEMA), False
        else:
            reply, hit = cache.ask(llm, system, text)
        all_cached = all_cached and hit
        for c in reply.get("clips", []) if isinstance(reply, dict) else []:
            clip = parse_clip(c)
            if clip and clip.end > clip.start:
                raw.append(clip)
    return raw, all_cached


def _fresh(raw: list[Clip], words: list[dict], min_len: float, max_len: float, exclude,
           bounds: Boundaries | None = None) -> list[Clip]:
    """Candidates snapped to whole thoughts, best first, overlapping neither each other nor existing clips."""
    bounds = bounds or Boundaries(words)
    snapped = [c for c in (_snap(Clip(**asdict(c)), words, min_len, max_len, bounds)
                           or (Clip(**asdict(c)) if c.reason.startswith(PEAK_REASON) else None) for c in raw) if c]
    snapped.sort(key=lambda c: c.score, reverse=True)
    unique: list[Clip] = []
    for c in snapped:
        if all(_overlap(c, o) < OVERLAP for o in unique) and all(_overlap(c, o) < OVERLAP for o in exclude):
            unique.append(c)
    return unique


@dataclass(frozen=True)
class EdgeVerdict:
    """One clip's answer from an edge check: move the edge ("extend" outward, "trim" inward) by whole sentences, or
    drop the last `words` of a recording that was cut off mid-sentence."""

    clip: int
    move: str
    sentences: int = 1
    words: int = 0


def _clip_span(bounds: Boundaries, c: Clip) -> tuple[int, int] | None:
    """First and last word of a clip, or None when no word falls inside it."""
    i0, i1 = bounds.index_at_start(c.start - 0.01), bounds.index_at_end(c.end)
    return (i0, i1) if i0 is not None and i1 is not None and i1 >= i0 else None


def _edge_verdicts(llm: LLM, prompt: str, lines: dict[int, str], what: str) -> list[EdgeVerdict]:
    """Asks an edge check about the clips described in `lines` (by clip index) and keeps only the well-formed moves;
    a broken or missing answer changes nothing."""
    if not lines:
        return []
    try:
        reply = llm.chat_json(prompt, "\n\n".join(lines.values()), think=False, schema=EDGES_SCHEMA)
    except Exception as exc:
        log(f"[hook] {what} check skipped: {exc}")
        return []
    verdicts = []
    for item in reply.get("clips", []) if isinstance(reply, dict) else []:
        try:
            k, move = int(item["id"]), str(item["verdict"])
            sentences, words = int(item.get("sentences") or 1), int(item.get("words") or 0)
        except (KeyError, ValueError, TypeError):
            continue
        if k in lines and move in ("extend", "trim"):
            verdicts.append(EdgeVerdict(k, move, max(1, min(MAX_STEPS, sentences)), max(0, words)))
    return verdicts


def _collides(moved: Clip, clip: Clip, others: list[Clip]) -> bool:
    return any(o is not clip and _overlap(moved, o) >= OVERLAP for o in others)


def _check_starts(llm: LLM, clips: list[Clip], words: list[dict], bounds: Boundaries, min_len: float,
                  max_len: float, language: str, neighbours: list[Clip] | None = None) -> None:
    """Each clip starts where its topic starts: a start may move back over whole sentences when the story or rant
    began before it (as long as the clip stays within the length limit and off the other clips), or forward over
    filler, never into the middle of a sentence."""
    spans, lines = {}, {}
    for k, c in enumerate(clips):
        span = _clip_span(bounds, c)
        if span is None:
            continue
        i0 = span[0]
        before = [i for i in bounds.open_at if i < i0][-MAX_STEPS:]
        spans[k] = (*span, before)
        units = [f"[{n - len(before)}] {_words_text(words[s:e])}"
                 for n, (s, e) in enumerate(zip(before, before[1:] + [i0]))]
        lines[k] = (f"id={k}\nBEFORE:\n" + ("\n".join(units) or "(the video starts here)") +
                    f"\nCLIP: {_words_text(words[i0:i0 + HEAD_WORDS])}")
    prompt = STARTS_PROMPT.format(language=language, max_steps=MAX_STEPS)
    for v in _edge_verdicts(llm, prompt, lines, "start"):
        clip, (i0, i1, before) = clips[v.clip], spans[v.clip]
        if v.move == "extend":
            fits = [i for i in before[-v.sentences:] if bounds.duration(i, i1) <= max_len + END_OVERFLOW]
            target = fits[0] if fits else None
        else:
            later = [i for i in bounds.open_at if i0 < i <= i1 and bounds.duration(i, i1) >= min_len]
            steps = min(v.sentences, 2)
            target = later[steps - 1] if len(later) >= steps else None
        if target is None or target == i0:
            continue
        if v.move == "extend" and _collides(Clip(start_time(words, target), clip.end, clip.score), clip, neighbours or clips):
            log(f"[hook] start kept: moving it back would run into another clip ({clip.start:.1f} s)")
            continue
        was = clip.start
        _place(clip, words, target, i1)
        log(f"[hook] start fixed: {v.move} ({was:.1f} s -> {clip.start:.1f} s)")


def _check_endings(llm: LLM, clips: list[Clip], words: list[dict], bounds: Boundaries, min_len: float,
                   max_len: float, language: str, neighbours: list[Clip] | None = None) -> list[Clip]:
    """The picker's last word on each ending: an end moves by whole sentences (to another complete boundary within
    the length limit) until the clip's topic is finished, never into the middle of one. Returns the clips whose end
    moved later, which may still need more."""
    spans = {k: span for k, c in enumerate(clips) if (span := _clip_span(bounds, c))}
    lines = {k: f"id={k}\n{_words_text(words[max(i0, i1 - TAIL_WORDS + 1):i1 + 1])}\nNEXT: "
                f"{_words_text(words[i1 + 1:i1 + 1 + NEXT_WORDS]) or '(the video ends)'}"
             for k, (i0, i1) in spans.items()}
    extended: list[Clip] = []
    prompt = ENDINGS_PROMPT.format(language=language, max_steps=MAX_STEPS)
    for v in _edge_verdicts(llm, prompt, lines, "ending"):
        clip, (i0, i1) = clips[v.clip], spans[v.clip]
        ends = bounds.complete_ends(i0, max_len + END_OVERFLOW)
        if v.move == "extend":
            later = [j for j in ends if j > i1]
            target = later[min(v.sentences, len(later)) - 1] if later else None
        elif i1 == len(words) - 1 and 0 < v.words:
            cut_off = v.words <= MAX_CUT_WORDS and not ends_sentence(words[i1]["word"])
            target = i1 - v.words if cut_off else None
        else:
            earlier = [j for j in ends if j < i1 and bounds.duration(i0, j) >= min_len]
            target = earlier[-1] if earlier else None
        if target is None or bounds.duration(i0, target) < 0.6 * min_len:
            continue
        if v.move == "extend" and _collides(Clip(clip.start, end_time(words, target), clip.score), clip, neighbours or clips):
            log(f"[hook] end kept: extending would run into another clip ({clip.end:.1f} s)")
            continue
        was = clip.end
        _place(clip, words, i0, target)
        log(f"[hook] end fixed: {v.move} ({was:.1f} s -> {clip.end:.1f} s)")
        if v.move == "extend":
            extended.append(clip)
    return extended


TEXTS_PROMPT = """You are a native {text_language} social media editor. Write the post texts of short clips cut
from the video "{title}". Each clip comes with what is said in it. For every clip:
{title_rule}
{alt_title_rule}
{description_rule}
{question_rule}
{topic_rule}
Write them in {text_language}, specific to that clip's own moment (never the video's title again).
{language_rule}
A clip with little or no speech comes with its REACTIONS (a loud moment, laughter, action on screen, a chat burst and
what the chat wrote): write from those and the video title what happens in it.
{dash_rule}
Return ONLY JSON: {{"clips": [{{"id": <id>, "title": "...", "alt_title": "...", "description": "...", "question": "...",
"topic": "..."}}]}}"""

TEXTS_SCHEMA = {
    "type": "object",
    "properties": {"clips": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": "integer"}, "title": {"type": "string"},
                                         "alt_title": {"type": "string"},
                                         "description": {"type": "string"}, "question": {"type": "string"},
                                         "topic": {"type": "string"}},
        "required": ["id", "title", "alt_title", "description", "question", "topic"]}}},
    "required": ["clips"],
}


def write_texts(llm: LLM, clips: list[Clip], words: list[dict], title: str, text_language: str,
                signals: Signals | None = None) -> int:
    """New title and description for clips that already exist (made before these texts, or to try other ones), or
    for moments picked by their reactions. Returns how many clips got new texts."""
    def about(c: Clip) -> str:
        found = signals.describe(c.start, c.end) if signals else ""
        return (_clip_text(c, words) or "(no speech)") + (f"\nREACTIONS: {found}" if found else "")

    lines = [f"id={i} | {c.duration:.0f}s\n{about(c)}" for i, c in enumerate(clips)]
    prompt = TEXTS_PROMPT.format(text_language=text_language, title=title.replace('"', "'"),
                                 title_rule=playbook.TITLE_RULE, description_rule=playbook.DESCRIPTION_RULE,
                                 dash_rule=playbook.DASH_RULE, question_rule=playbook.QUESTION_RULE,
                                 topic_rule=playbook.TOPIC_RULE, alt_title_rule=playbook.ALT_TITLE_RULE,
                                 language_rule=playbook.HUNGARIAN_RULE if text_language == "Hungarian" else "")
    reply = llm.chat_json(prompt, "\n\n".join(lines), think=think_enabled(), schema=TEXTS_SCHEMA)
    changed = 0
    for item in reply.get("clips", []) if isinstance(reply, dict) else []:
        try:
            c = clips[int(item["id"])]
            new_title, new_description = post_clean(str(item["title"]))[:100], post_clean(str(item["description"]))
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        if new_title:
            c.title, c.description = new_title, new_description[:300]
            c.question = post_clean(str(item.get("question") or c.question))[:160]
            c.topic = post_clean(str(item.get("topic") or c.topic))[:40]
            c.alt_title = post_clean(str(item.get("alt_title") or c.alt_title))[:100]
            changed += 1
    one_topic(clips)
    if text_language == "Hungarian" and changed:
        proofread(llm, clips, text_language)
    return changed


def one_topic(clips: list[Clip]) -> None:
    """Every clip of a video follows for the same subject: the shortest one the model named (the video's subject
    rather than one clip's detail, e.g. "GTA 6" over "GTA 6 character design")."""
    topics = [c.topic for c in clips if c.topic]
    if topics:
        common = min(topics, key=lambda t: (len(t.split()), len(t)))
        for c in clips:
            c.topic = common


def text_language_name(code: str | None, spoken: str | None) -> str:
    return LANG_NAMES.get(code or "", LANG_NAMES.get(spoken or "", spoken or "English"))


PROOFREAD_PROMPT = """You are a native Hungarian copy editor for a Hungarian creator's short videos. Proofread every
text: fix spelling, accents (ő, ű), grammar, agreement and punctuation, and rewrite anything that sounds translated
from English into natural spoken Hungarian. Keep the meaning, the names and the length (never longer); no emojis.
Titles in sentence case. Hungarian quotation marks („…”). No em or en dashes.
Return ONLY JSON: {"texts": [{"id": <id>, "text": "..."}]} with every id, unchanged texts included."""
PROOFREAD_SCHEMA = {
    "type": "object",
    "properties": {"texts": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": "integer"}, "text": {"type": "string"}},
        "required": ["id", "text"]}}},
    "required": ["texts"],
}
PROOFREAD_FIELDS = ("title", "alt_title", "description", "question", "hook", "alt_hook")


def proofread(llm: LLM, clips: list[Clip], language: str) -> int:
    """One pass of a native editor over every Hungarian text of the clips; returns how many texts changed."""
    texts = [(c, f) for c in clips for f in PROOFREAD_FIELDS if getattr(c, f)]
    if not texts:
        return 0
    user = "\n".join(f"id={i} ({field}): {getattr(c, field)}" for i, (c, field) in enumerate(texts))
    try:
        reply = llm.chat_json(PROOFREAD_PROMPT, user, think=False, schema=PROOFREAD_SCHEMA)
    except Exception as exc:
        log(f"[hook] proofreading skipped: {exc}")
        return 0
    changed = 0
    for item in reply.get("texts", []) if isinstance(reply, dict) else []:
        try:
            c, field = texts[int(item["id"])]
            new = post_clean(str(item["text"]), language)
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        old = getattr(c, field)
        if new and new != old and len(new) <= int(1.3 * len(old)) + 10:
            setattr(c, field, new)
            changed += 1
    if changed:
        log(f"[hook] Hungarian proofreading: {changed} text(s) improved")
    return changed


POLISH_PROMPT = """You are a copy editor for viral short-form video hooks in {language}.
Rewrite each hook to at most {max_words} words (ideally 4-6), keeping its exact meaning and names.
Punchy, natural, grammatical {language}, creates curiosity, no clickbait lies, no emojis, no hashtags,
no "Name:" prefix.
Return ONLY JSON: {{"hooks": [{{"id": <id>, "hook": "..."}}]}}"""


def _polish_hooks(llm: LLM, clips: list[Clip], language: str, max_words: int = playbook.HOOK_MAX_WORDS,
                  max_chars: int = 46) -> None:
    """LLMs ignore length limits; overlays longer than ~7 words are not read in 3 seconds. Tighten them."""
    texts = [(c, attr) for c in clips for attr in ("hook", "alt_hook") if getattr(c, attr)]
    long_ones = [(i, c, attr) for i, (c, attr) in enumerate(texts)
                 if len(getattr(c, attr).split()) > max_words or len(getattr(c, attr)) > max_chars]
    if not long_ones:
        return
    user = "\n".join(f'id={i}: "{getattr(c, attr)}"' for i, c, attr in long_ones)
    try:
        reply = llm.chat_json(POLISH_PROMPT.format(language=language, max_words=max_words), user, schema=POLISH_SCHEMA)
    except ClippyError as exc:
        log(f"[hook] shortening skipped: {exc}")
        return
    for item in reply.get("hooks", []) if isinstance(reply, dict) else []:
        try:
            i, hook = int(item["id"]), post_clean(str(item["hook"]))
        except (KeyError, ValueError, TypeError):
            continue
        if 0 <= i < len(texts) and hook and len(hook.split()) <= max_words + 2:
            c, attr = texts[i]
            log(f"[hook] shortened: {getattr(c, attr)!r} -> {hook!r}")
            setattr(c, attr, hook)


def parse_clip(c: dict) -> Clip | None:
    """One clip from the LLM's JSON; unknown hook types and risks are dropped, risks lower the score."""
    try:
        risks = playbook.known_risks(c.get("risks") if isinstance(c.get("risks"), list) else ())
        clip = Clip(
            start=float(c["start"]), end=float(c["end"]),
            score=playbook.adjusted_score(float(c.get("score", 50)), risks),
            hook=playbook.tidy_hook(str(c.get("hook", ""))), title=str(c.get("title", "")).strip(),
            alt_title=str(c.get("alt_title", "")).strip()[:100],
            hashtags=[str(h) for h in c.get("hashtags", [])][:5], reason=str(c.get("reason", "")).strip(),
            hook_type=playbook.known_hook_type(c.get("hook_type")),
            alt_hook=playbook.tidy_hook(str(c.get("alt_hook", ""))),
            question=str(c.get("question", "")).strip(), risks=risks,
            description=str(c.get("description", "")).strip()[:300], topic=str(c.get("topic", "")).strip()[:40],
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if clip.alt_hook.casefold() == clip.hook.casefold():
        clip.alt_hook = ""
    if not clip.alt_title or clip.alt_title.casefold() == clip.title.casefold():
        clip.alt_title = clip.alt_hook if clip.alt_hook.casefold() != clip.title.casefold() else ""
    return clip


@dataclass(frozen=True)
class PickCache:
    """The picker's answer for one transcript window, kept per video (data/cache/picks/<video>.<key>.json). The
    same window, prompt and model give the same answer, so "more clips" reads instead of asking the LLM again; a
    new model, prompt, wish or transcript is a new key."""

    folder: Path
    stem: str

    def _path(self, llm: LLM, system: str, text: str) -> Path:
        key = hashlib.sha256(f"{llm.describe()}\n{system}\n{text}".encode("utf-8")).hexdigest()[:24]
        return self.folder / f"{self.stem}.{key}.json"

    def ask(self, llm: LLM, system: str, text: str) -> tuple[dict, bool]:
        """The window's reply and whether it came from the cache."""
        path = self._path(llm, system, text)
        try:
            return json.loads(path.read_text(encoding="utf-8")), True
        except (OSError, ValueError):
            pass
        reply = llm.chat_json(system, text, think=think_enabled(), schema=CLIPS_SCHEMA)
        if isinstance(reply, dict) and reply.get("clips"):
            write_json(path, reply)
        return reply, False


def _avoiding(exclude) -> str:
    spans = ", ".join(f"{fmt_ts(c.start)}-{fmt_ts(c.end)}" for c in exclude)
    return (f"\n\nThese moments are already clips; pick DIFFERENT moments that do not overlap them: {spans}"
            if spans else "")


def find_highlights(transcript: dict, llm: LLM, max_clips: int = 5, min_len: float = 20,
                    max_len: float = 60, window_sec: float = 480, overlap_sec: float = 60,
                    title: str = "", exclude: list[Clip] | tuple = (), instructions: str = "",
                    text_language: str | None = None, cache: PickCache | None = None,
                    signals: Signals | None = None, lessons: str = "", speakers=None) -> list[Clip]:
    """`exclude`: clips that already exist (new picks must not overlap them).
    `instructions`: free-text wishes from the user ("the funniest parts", "only the parts about music"…).
    `cache`: earlier window answers for this video; when they no longer hold enough new moments, the windows are
    asked again, told which moments are taken.
    `signals`: the video's reactions (sound, picture, chat); they mark lines for the picker and add candidates."""
    words = [w for s in transcript["segments"] for w in s["words"]]
    signals = signals if signals is not None and signals.peaks(1) else None
    if not words and signals is None:
        raise ClippyError(key="error.empty_transcript")
    sents = sentences(words)
    turns = speakers(sents) if speakers else []
    if len(set(t for t in turns if t)) >= 2:
        sents = [{**s, "speaker": t} for s, t in zip(sents, turns)]
    language = LANG_NAMES.get(transcript.get("language") or "", transcript.get("language") or "the transcript's")
    text_language = LANG_NAMES.get(text_language or "", language)

    per_window = max(2, min(5, max_clips))
    raw: list[Clip] = []
    if llm.backend == "none":
        raw = _heuristic(sents, min_len, max_len, signals)
    else:
        system = SYSTEM_PROMPT.format(rubric=playbook.selection_rules(int(min_len), int(max_len)),
                                      writing=playbook.writing_rules(text_language), per_window=per_window,
                                      title=title.replace('"', "'"), language=language, text_language=text_language)
        if any(s.get("speaker") for s in sents):
            system += "\n" + SPEAKERS_RULE
        if signals is not None:
            system += "\n" + SIGNALS_RULE
        if lessons:
            system += LESSONS_RULE + lessons
        if instructions.strip():
            system += ("\n\nThe channel owner's specific wishes for THIS selection (follow them, they override "
                       f"the generic preferences above):\n{instructions.strip()}")
        wins = _windows(sents, window_sec, overlap_sec)
        raw, cached = _ask_windows(llm, system, wins, cache, signals)
        if exclude and cached and len(_fresh(raw, words, min_len, max_len, exclude)) < max_clips:
            log("[hook] the earlier picks are used up: asking for new moments")
            raw += _ask_windows(llm, system + _avoiding(exclude), wins, cache, signals)[0]
    if signals is not None:
        duration = float(transcript.get("duration") or signals.length * signals.hop)
        peaks = _peak_clips(signals, raw, min_len, max_len, duration, 3 * max_clips)
        if peaks:
            log(f"[hook] {len(peaks)} more candidates from reactions (sound, picture, chat)")
        raw += peaks

    bounds = Boundaries(words)
    unique = _fresh(raw, words, min_len, max_len, exclude, bounds)

    if llm.backend != "none" and len(unique) > max_clips:
        log(f"[hook] globally re-ranking {len(unique)} candidates…")
        with report_span(0.8, 0.9):
            unique = _rerank(llm, unique, words, max_clips, title, language, text_language, signals, lessons)

    chosen = unique[:max_clips]
    spoken = [c for c in chosen if _speaks(c, words)]
    if llm.backend != "none" and spoken:
        _check_starts(llm, spoken, words, bounds, min_len, max_len, language, chosen)
        again = spoken
        for _ in range(CHECK_ROUNDS):
            again = _check_endings(llm, again, words, bounds, min_len, max_len, language, chosen) if again else []
    untitled = [c for c in chosen if not c.description]
    if llm.backend != "none" and untitled:
        try:
            write_texts(llm, untitled, words, title, text_language, signals)
        except ClippyError as exc:
            log(f"[hook] texts of the reaction clips skipped: {exc}")
    for c in untitled:
        c.title = c.title or title[:70]
    for c in chosen:
        c.hook, c.title, c.alt_title, c.alt_hook, c.question, c.description, c.topic = (
            post_clean(x, text_language) for x in (c.hook, c.title, c.alt_title, c.alt_hook, c.question, c.description,
                                                   c.topic))
    one_topic(chosen)
    if llm.backend != "none":
        with report_span(0.9, 1.0):
            _polish_hooks(llm, chosen, text_language)
            if text_language == "Hungarian" and chosen:
                proofread(llm, chosen, text_language)
    log(f"[hook] {len(raw)} candidates -> {len(chosen)} clips selected")
    return chosen
