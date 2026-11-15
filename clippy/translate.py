"""Captions in another language than the spoken one: the clip's caption lines are translated by the LLM and the
translated words are spread over the original lines' timing, so they still appear in sync with the speech."""

from __future__ import annotations

import json

from .i18n import NAMES
from .llm import LLM
from .text import clean_text, ends_sentence
from .tools import ClippyError, log

MAX_LINE_WORDS = 14
SYSTEM_PROMPT = """You translate the on-screen text of a short vertical video into {language}.
You get JSON with a "hook" (the big text shown in the first seconds) and numbered "lines" (the spoken captions).
Translate every line into natural, spoken, grammatical {language} as a native subtitler would: keep the meaning,
names and numbers, keep it about as short as the original, no explanations, no quotation marks around lines.
The hook stays punchy (max 8 words). Translate EACH line on its own and keep its "id": never merge or split lines,
even when a sentence continues on the next line. If a text is already in {language}, return it unchanged.
Return ONLY JSON: {{"hook": "...", "lines": [{{"id": 0, "text": "..."}}, ...]}}"""
SCHEMA = {
    "type": "object",
    "properties": {
        "hook": {"type": "string"},
        "lines": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "integer"}, "text": {"type": "string"}}, "required": ["id", "text"]}},
    },
    "required": ["hook", "lines"],
}


def caption_lines(words: list[dict]) -> list[list[dict]]:
    """Sentence-sized groups of timed words (long sentences are split so a line stays readable)."""
    lines: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        cur.append(w)
        if ends_sentence(w["word"]) or len(cur) >= MAX_LINE_WORDS:
            lines.append(cur)
            cur = []
    if cur:
        lines.append(cur)
    return lines


def spread(text: str, line: list[dict]) -> list[dict]:
    """Translated words over the original line's time span, each in proportion to its length."""
    tokens = clean_text(text).split()
    if not tokens:
        return []
    t0, t1 = line[0]["start"], line[-1]["end"]
    weights = [len(t) + 2 for t in tokens]
    total, acc, out = float(sum(weights)), 0.0, []
    for token, weight in zip(tokens, weights):
        start = t0 + (t1 - t0) * acc / total
        acc += weight
        out.append({"start": round(start, 3), "end": round(t0 + (t1 - t0) * acc / total, 3), "word": token})
    return out


class CaptionTranslator:
    """Callable used by the renderer: (timed words, hook) -> (translated timed words, translated hook)."""

    def __init__(self, llm: LLM, target: str):
        self.llm, self.target = llm, target

    def __call__(self, words: list[dict], hook: str) -> tuple[list[dict], str]:
        lines = caption_lines(words)
        if not lines and not hook:
            return words, hook
        payload = {"hook": hook, "lines": [{"id": i, "text": " ".join(w["word"] for w in line)}
                                           for i, line in enumerate(lines)]}
        try:
            reply = self.llm.chat_json(SYSTEM_PROMPT.format(language=NAMES[self.target]),
                                       json.dumps(payload, ensure_ascii=False), think=False, schema=SCHEMA)
        except ClippyError as exc:
            log(f"[render] caption translation skipped: {exc}")
            return words, hook
        texts = {}
        for item in reply.get("lines", []) if isinstance(reply, dict) else []:
            try:
                texts[int(item["id"])] = str(item["text"])
            except (KeyError, TypeError, ValueError):
                continue
        translated = [w for i, line in enumerate(lines) for w in (spread(texts[i], line) if texts.get(i) else line)]
        new_hook = clean_text(str(reply.get("hook") or hook)) if hook and isinstance(reply, dict) else hook
        missing = sum(1 for i in range(len(lines)) if not texts.get(i))
        log(f"[render] captions translated to {NAMES[self.target]}"
            + (f" ({missing} of {len(lines)} lines kept as spoken)" if missing else ""))
        return translated or words, new_hook
