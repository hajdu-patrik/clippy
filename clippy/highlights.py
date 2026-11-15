"""Clip selection: the LLM scores sentence-level transcript windows, then re-ranks and polishes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from . import playbook
from .llm import LLM, think_enabled
from .text import clean_text, ends_sentence
from .tools import ClippyError, fmt_ts, log, report, report_span

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
"hook_type": "...", "alt_hook": "...", "question": "...", "title": "...", "hashtags": ["#..."], "risks": [],
"reason": "..."}}]}}"""

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
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "risks": {"type": "array", "items": {"type": "string", "enum": list(playbook.RISK_KEYS)}},
                    "reason": {"type": "string"},
                },
                "required": ["start", "end", "score", "hook", "hook_type", "alt_hook", "question", "title", "hashtags",
                             "risks", "reason"],
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


def _snap(clip: Clip, words: list[dict], min_len: float, max_len: float) -> Clip | None:
    """Align a clip to sentence ends inside the length bounds, never into a neighbour word."""
    if not words:
        return None
    starts = [i for i, w in enumerate(words) if w["start"] >= clip.start - 0.35]
    if not starts:
        return None
    i0 = starts[0]
    i1 = i0
    while i1 + 1 < len(words) and words[i1 + 1]["end"] <= clip.end + 0.35:
        i1 += 1

    def dur(a: int, b: int) -> float:
        return words[b]["end"] - words[a]["start"]

    if dur(i0, i1) > max_len:
        j = i1
        while j > i0 and dur(i0, j) > max_len:
            j -= 1
        k = j
        while k > i0 and not ends_sentence(words[k]["word"]) and dur(i0, k) > min_len:
            k -= 1
        i1 = k if ends_sentence(words[k]["word"]) and dur(i0, k) >= min_len else j
    while dur(i0, i1) < min_len and i1 + 1 < len(words):
        i1 += 1
        if dur(i0, i1) >= min_len and ends_sentence(words[i1]["word"]):
            break
    if dur(i0, i1) < min_len * 0.6:
        return None

    prev_end = words[i0 - 1]["end"] + 0.04 if i0 > 0 else 0.0
    next_start = words[i1 + 1]["start"] - 0.06 if i1 + 1 < len(words) else words[i1]["end"] + 0.5
    clip.start = round(max(0.0, min(words[i0]["start"], max(words[i0]["start"] - 0.2, prev_end))), 3)
    clip.end = round(max(words[i1]["end"], min(words[i1]["end"] + 0.4, next_start)), 3)
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
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if clip.alt_hook.casefold() == clip.hook.casefold():
        clip.alt_hook = ""
    return clip


def find_highlights(transcript: dict, llm: LLM, max_clips: int = 5, min_len: float = 20,
                    max_len: float = 60, window_sec: float = 480, overlap_sec: float = 60,
                    title: str = "", exclude: list[Clip] | tuple = (), instructions: str = "",
                    text_language: str | None = None) -> list[Clip]:
    """`exclude`: clips that already exist (new picks must not overlap them).
    `instructions`: free-text wishes from the user ("the funniest parts", "only the parts about music"…)."""
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
        for n, win in enumerate(wins, 1):
            report(0.8 * (n - 1) / len(wins))
            log(f"[hook] window {n}/{len(wins)}  ({fmt_ts(win[0]['start'])}–{fmt_ts(win[-1]['end'])})  {llm.describe()}")
            reply = llm.chat_json(system, "TRANSCRIPT:\n" + _format_window(win), think=think_enabled(), schema=CLIPS_SCHEMA)
            for c in reply.get("clips", []) if isinstance(reply, dict) else []:
                clip = parse_clip(c)
                if clip and clip.end > clip.start:
                    raw.append(clip)

    snapped = [c for c in (_snap(c, words, min_len, max_len) for c in raw) if c]
    snapped.sort(key=lambda c: c.score, reverse=True)
    unique: list[Clip] = []
    for c in snapped:
        if all(_overlap(c, o) < 0.15 for o in unique) and all(_overlap(c, o) < 0.15 for o in exclude):
            unique.append(c)

    if llm.backend != "none" and len(unique) > max_clips:
        log(f"[hook] globally re-ranking {len(unique)} candidates…")
        with report_span(0.8, 0.9):
            unique = _rerank(llm, unique, words, max_clips, title, language, text_language)

    chosen = unique[:max_clips]
    for c in chosen:
        c.hook, c.title, c.alt_hook, c.question = map(clean_text, (c.hook, c.title, c.alt_hook, c.question))
    if llm.backend != "none":
        with report_span(0.9, 1.0):
            _polish_hooks(llm, chosen, text_language)
    log(f"[hook] {len(raw)} candidates -> {len(chosen)} clips selected")
    return chosen
