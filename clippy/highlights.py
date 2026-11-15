"""Clip selection: the LLM scores sentence-level transcript windows, then re-ranks and polishes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import playbook
from .boundaries import END_OVERFLOW, Boundaries, end_time, start_time
from .llm import LLM, think_enabled
from .text import clean_text, ends_sentence
from .tools import ClippyError, fmt_ts, log, report, report_span, write_json

LANG_NAMES = {"hu": "Hungarian", "en": "English", "de": "German", "es": "Spanish", "fr": "French",
              "it": "Italian", "pl": "Polish", "ro": "Romanian", "sk": "Slovak", "cs": "Czech"}

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
"hook_type": "...", "alt_hook": "...", "question": "...", "title": "...", "description": "...", "hashtags": ["#..."],
"risks": [], "reason": "..."}}]}}"""

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
                    "description": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "risks": {"type": "array", "items": {"type": "string", "enum": list(playbook.RISK_KEYS)}},
                    "reason": {"type": "string"},
                },
                "required": ["start", "end", "score", "hook", "hook_type", "alt_hook", "question", "title",
                             "description", "hashtags", "risks", "reason"],
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
    hashtags: list[str] = field(default_factory=list)
    reason: str = ""
    hook_type: str = ""
    alt_hook: str = ""
    question: str = ""
    description: str = ""
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


def _format_window(win: list[dict]) -> str:
    return "\n".join(f"[{s['start']:.2f}-{s['end']:.2f}] {s['text']}" for s in win)


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


def _heuristic(sents: list[dict], min_len: float, max_len: float) -> list[Clip]:
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
        cands.append(Clip(s["start"], sents[j]["end"], round(min(95, max(5, score)), 1),
                          hook=" ".join(s["text"].split()[:7]), title=s["text"][:70],
                          reason="heuristic: lively speech"))
    return cands


def _clip_text(c: Clip, words: list[dict], limit: int = 700) -> str:
    text = " ".join(w["word"] for w in words if c.start - 0.05 <= w["start"] <= c.end)
    return text[:limit] + ("…" if len(text) > limit else "")


def _rerank(llm: LLM, cands: list[Clip], words: list[dict], n: int, title: str, language: str,
            text_language: str) -> list[Clip]:
    lines = [
        f"id={i} | {c.duration:.0f}s | first score {c.score:.0f} | hook: {c.hook}"
        f"{' | risks: ' + ', '.join(c.risks) if c.risks else ''}\n{_clip_text(c, words)}"
        for i, c in enumerate(cands)
    ]
    try:
        prompt = RERANK_PROMPT.format(title=title, language=language, text_language=text_language, n=n,
                                      max_words=playbook.HOOK_MAX_WORDS)
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
A good ending lands the punchline, the conclusion or the answer. A bad ending stops on a set-up, a list that goes
on, a promise or a question that the next sentences answer, or it adds the first sentence of a new topic.
For each clip answer:
- "ok": the ending is complete;
- "extend" with "sentences" 1 or 2: the next sentence(s) finish this clip's thought;
- "trim": the clip's last sentence already starts something new that the clip does not need.
Return ONLY JSON: {{"clips": [{{"id": <id>, "verdict": "ok" | "extend" | "trim", "sentences": 1}}]}}"""

ENDINGS_SCHEMA = {
    "type": "object",
    "properties": {"clips": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": "integer"},
                                         "verdict": {"type": "string", "enum": ["ok", "extend", "trim"]},
                                         "sentences": {"type": "integer"}},
        "required": ["id", "verdict"]}}},
    "required": ["clips"],
}
TAIL_WORDS, NEXT_WORDS = 45, 40


def _words_text(words: list[dict]) -> str:
    return " ".join(w["word"] for w in words)


def _ask_windows(llm: LLM, system: str, wins: list[list[dict]], cache: PickCache | None) -> tuple[list[Clip], bool]:
    """Every window's candidates, and whether all of them came from the cache."""
    raw, all_cached = [], True
    for n, win in enumerate(wins, 1):
        report(0.8 * (n - 1) / len(wins))
        log(f"[hook] window {n}/{len(wins)}  ({fmt_ts(win[0]['start'])}–{fmt_ts(win[-1]['end'])})  {llm.describe()}")
        text = "TRANSCRIPT:\n" + _format_window(win)
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
    snapped = [c for c in (_snap(Clip(**asdict(c)), words, min_len, max_len, bounds) for c in raw) if c]
    snapped.sort(key=lambda c: c.score, reverse=True)
    unique: list[Clip] = []
    for c in snapped:
        if all(_overlap(c, o) < 0.15 for o in unique) and all(_overlap(c, o) < 0.15 for o in exclude):
            unique.append(c)
    return unique


def _check_endings(llm: LLM, clips: list[Clip], words: list[dict], bounds: Boundaries, min_len: float,
                   max_len: float, language: str) -> None:
    """The picker's last word on each ending: an end may move by whole sentences (to another complete boundary
    within the length limits), never into the middle of one."""
    spans = []
    for c in clips:
        i0, i1 = bounds.index_at_start(c.start - 0.01), bounds.index_at_end(c.end)
        spans.append((i0, i1) if i0 is not None and i1 is not None and i1 >= i0 else None)
    lines = [f"id={k}\n{_words_text(words[max(i0, i1 - TAIL_WORDS + 1):i1 + 1])}\nNEXT: "
             f"{_words_text(words[i1 + 1:i1 + 1 + NEXT_WORDS]) or '(the video ends)'}"
             for k, span in enumerate(spans) if span for i0, i1 in [span]]
    if not lines:
        return
    try:
        reply = llm.chat_json(ENDINGS_PROMPT.format(language=language), "\n\n".join(lines), think=False,
                              schema=ENDINGS_SCHEMA)
    except Exception as exc:
        log(f"[hook] ending check skipped: {exc}")
        return
    for item in reply.get("clips", []) if isinstance(reply, dict) else []:
        try:
            k, verdict = int(item["id"]), str(item["verdict"])
            span, clip = spans[k], clips[k]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        if span is None or verdict not in ("extend", "trim"):
            continue
        i0, i1 = span
        ends = bounds.complete_ends(i0, max_len + END_OVERFLOW)
        if verdict == "extend":
            later = [j for j in ends if j > i1]
            steps = max(1, min(2, int(item.get("sentences") or 1)))
            target = later[min(steps, len(later)) - 1] if later else None
        else:
            earlier = [j for j in ends if j < i1 and bounds.duration(i0, j) >= min_len]
            target = earlier[-1] if earlier else None
        if target is None:
            continue
        before = clip.end
        _place(clip, words, i0, target)
        log(f"[hook] end fixed: {verdict} ({before:.1f} s -> {clip.end:.1f} s)")


TEXTS_PROMPT = """You are a native {text_language} social media editor. Write the post texts of short clips cut
from the video "{title}". Each clip comes with what is said in it. For every clip:
{title_rule}
{description_rule}
Write both in {text_language}, specific to that clip's own moment (never the video's title again).
Return ONLY JSON: {{"clips": [{{"id": <id>, "title": "...", "description": "..."}}]}}"""

TEXTS_SCHEMA = {
    "type": "object",
    "properties": {"clips": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": "integer"}, "title": {"type": "string"},
                                         "description": {"type": "string"}},
        "required": ["id", "title", "description"]}}},
    "required": ["clips"],
}


def write_texts(llm: LLM, clips: list[Clip], words: list[dict], title: str, text_language: str) -> int:
    """New title and description for clips that already exist (made before these texts, or to try other ones).
    Returns how many clips got new texts."""
    lines = [f"id={i} | {c.duration:.0f}s\n{_clip_text(c, words)}" for i, c in enumerate(clips)]
    prompt = TEXTS_PROMPT.format(text_language=text_language, title=title.replace('"', "'"),
                                 title_rule=playbook.TITLE_RULE, description_rule=playbook.DESCRIPTION_RULE)
    reply = llm.chat_json(prompt, "\n\n".join(lines), think=think_enabled(), schema=TEXTS_SCHEMA)
    changed = 0
    for item in reply.get("clips", []) if isinstance(reply, dict) else []:
        try:
            c = clips[int(item["id"])]
            new_title, new_description = clean_text(str(item["title"]))[:100], clean_text(str(item["description"]))
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        if new_title:
            c.title, c.description = new_title, new_description[:300]
            changed += 1
    return changed


def text_language_name(code: str | None, spoken: str | None) -> str:
    return LANG_NAMES.get(code or "", LANG_NAMES.get(spoken or "", spoken or "English"))


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
            i, hook = int(item["id"]), clean_text(str(item["hook"]))
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
            hashtags=[str(h) for h in c.get("hashtags", [])][:5], reason=str(c.get("reason", "")).strip(),
            hook_type=playbook.known_hook_type(c.get("hook_type")),
            alt_hook=playbook.tidy_hook(str(c.get("alt_hook", ""))),
            question=str(c.get("question", "")).strip(), risks=risks,
            description=str(c.get("description", "")).strip()[:300],
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if clip.alt_hook.casefold() == clip.hook.casefold():
        clip.alt_hook = ""
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
    spans = ", ".join(f"{fmt_ts(c.start)}–{fmt_ts(c.end)}" for c in exclude)
    return (f"\n\nThese moments are already clips; pick DIFFERENT moments that do not overlap them: {spans}"
            if spans else "")


def find_highlights(transcript: dict, llm: LLM, max_clips: int = 5, min_len: float = 20,
                    max_len: float = 60, window_sec: float = 480, overlap_sec: float = 60,
                    title: str = "", exclude: list[Clip] | tuple = (), instructions: str = "",
                    text_language: str | None = None, cache: PickCache | None = None) -> list[Clip]:
    """`exclude`: clips that already exist (new picks must not overlap them).
    `instructions`: free-text wishes from the user ("the funniest parts", "only the parts about music"…).
    `cache`: earlier window answers for this video; when they no longer hold enough new moments, the windows are
    asked again, told which moments are taken."""
    words = [w for s in transcript["segments"] for w in s["words"]]
    if not words:
        raise ClippyError(key="error.empty_transcript")
    sents = sentences(words)
    language = LANG_NAMES.get(transcript.get("language") or "", transcript.get("language") or "the transcript's")
    text_language = LANG_NAMES.get(text_language or "", language)

    per_window = max(2, min(5, max_clips))
    raw: list[Clip] = []
    if llm.backend == "none":
        raw = _heuristic(sents, min_len, max_len)
    else:
        system = SYSTEM_PROMPT.format(rubric=playbook.selection_rules(int(min_len), int(max_len)),
                                      writing=playbook.writing_rules(text_language), per_window=per_window,
                                      title=title.replace('"', "'"), language=language, text_language=text_language)
        if instructions.strip():
            system += ("\n\nThe channel owner's specific wishes for THIS selection (follow them, they override "
                       f"the generic preferences above):\n{instructions.strip()}")
        wins = _windows(sents, window_sec, overlap_sec)
        raw, cached = _ask_windows(llm, system, wins, cache)
        if exclude and cached and len(_fresh(raw, words, min_len, max_len, exclude)) < max_clips:
            log("[hook] the earlier picks are used up: asking for new moments")
            raw += _ask_windows(llm, system + _avoiding(exclude), wins, cache)[0]

    bounds = Boundaries(words)
    unique = _fresh(raw, words, min_len, max_len, exclude, bounds)

    if llm.backend != "none" and len(unique) > max_clips:
        log(f"[hook] globally re-ranking {len(unique)} candidates…")
        with report_span(0.8, 0.9):
            unique = _rerank(llm, unique, words, max_clips, title, language, text_language)

    chosen = unique[:max_clips]
    if llm.backend != "none" and chosen:
        _check_endings(llm, chosen, words, bounds, min_len, max_len, language)
    for c in chosen:
        c.hook, c.title, c.alt_hook, c.question, c.description = map(
            clean_text, (c.hook, c.title, c.alt_hook, c.question, c.description))
    if llm.backend != "none":
        with report_span(0.9, 1.0):
            _polish_hooks(llm, chosen, text_language)
    log(f"[hook] {len(raw)} candidates -> {len(chosen)} clips selected")
    return chosen
