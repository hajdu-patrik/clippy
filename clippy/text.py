"""Word / sentence helpers shared by clip selection, the tight edit and the captions."""

from __future__ import annotations

import re

_CLOSERS = "\"”»)'"
FILLERS = {"ö", "öö", "ööö", "őő", "ööh", "öhm", "hmm", "hm", "mhm", "mm", "eee", "ee", "e",
           "um", "umm", "uh", "uhm", "erm", "er", "ah", "eh"}


def ends_sentence(word: str) -> bool:
    return word.rstrip(_CLOSERS).endswith((".", "!", "?", "…"))


def ends_clause(word: str) -> bool:
    """Sentence end or a comma / colon: a natural place to break a caption line."""
    return word.rstrip(_CLOSERS).endswith((".", "!", "?", "…", ",", ":"))


def is_filler(word: str) -> bool:
    return re.sub(r"[^\wáéíóöőúüű]", "", word.lower()) in FILLERS


def clean_text(text: str) -> str:
    """Whisper/LLM tokenisation fixes: '10 -ben' -> '10-ben', double spaces, space before punctuation."""
    text = re.sub(r"(\w) -(\w)", r"\1-\2", text)
    text = re.sub(r"\s+([,.!?…:;])", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


_RANGE_DASH = re.compile(r"(?<=[\d}])\s*[–—]\s*(?=[\d{])")
_DASH = re.compile(r"\s*[–—]+\s*")


def no_dashes(text: str) -> str:
    """Post and on-screen texts never use the long dashes typical of AI writing: a range gets a hyphen (20-45), a
    dash between words becomes a comma."""
    text = _RANGE_DASH.sub("-", text or "")
    text = _DASH.sub(", ", text).strip(" ,")
    return re.sub(r",\s*([,.!?…:;])", r"\1", text)


def post_clean(text: str) -> str:
    return no_dashes(clean_text(text))


def merge_suffixes(words: list[dict]) -> list[dict]:
    """Whisper splits Hungarian number suffixes ('10', '-ben'): glue them back into one caption token."""
    out: list[dict] = []
    for w in words:
        if out and w["word"].startswith("-") and len(w["word"]) > 1 and w["start"] - out[-1]["end"] < 0.3:
            out[-1] = {**out[-1], "word": out[-1]["word"] + w["word"], "end": w["end"]}
        else:
            out.append(dict(w))
    return out
