"""Word / sentence helpers shared by clip selection, the tight edit and the captions."""

from __future__ import annotations

import re
import unicodedata

_CLOSERS = "\"”»)'"
ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "prof", "st", "vs", "jr", "sr", "mt", "e.g", "i.e", "approx", "dept", "fig", "gen", "gov",
    "sgt", "capt", "lt", "col", "rev", "pl", "kb", "ill", "un", "ifj", "ozv", "vo", "ld", "tel",
})
FILLERS = {"ö", "öö", "ööö", "őő", "ööh", "öhm", "hmm", "hm", "mhm", "mm", "eee", "ee", "e",
           "um", "umm", "uh", "uhm", "erm", "er", "ah", "eh"}


def fold(text: str) -> str:
    """Lowercase without accents, so "Legújabb" and "legujabb" are the same word."""
    plain = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in plain if not unicodedata.combining(c)).lower()


def unsymbolled(text: str) -> str:
    """Text without emoji and other pictographs (and their joiners and variation selectors)."""
    return "".join(c for c in text or "" if unicodedata.category(c) not in ("So", "Sk", "Cf", "Cs", "Co")
                   and not 0xFE00 <= ord(c) <= 0xFE0F)


def hint_words(title: str) -> str:
    """A video title as words Whisper may expect: no emoji, @handles, #hashtags or links (a title full of them makes
    Whisper write them into the speech)."""
    text = re.sub(r"https?://\S+|[@#]\w+", " ", unsymbolled(title))
    return " ".join(re.sub(r"[|•·]+", " ", text).split())


def spoken(words: list[dict]) -> list[dict]:
    """Words as said: emoji and pictographs taken out, and a "word" that was nothing else dropped."""
    out = []
    for w in words:
        text = unsymbolled(w["word"]).strip()
        if any(c.isalnum() for c in text):
            out.append(w if text == w["word"] else {**w, "word": text})
    return out


def ends_sentence(word: str, following: str | None = None) -> bool:
    """Whether a word closes a sentence. A dot after an abbreviation ("Dr.", "pl.") or an initial ("J.") does not,
    nor one after a number that the next word, in lower case, carries on (a Hungarian ordinal: "2026. november")."""
    bare = word.rstrip(_CLOSERS)
    if not bare.endswith((".", "!", "?", "…")):
        return False
    if bare.endswith(".") and not bare.endswith("..."):
        stem = bare[:-1].lstrip("\"“„'(")
        if fold(stem) in ABBREVIATIONS or re.fullmatch(r"[A-ZÁÉÍÓÖŐÚÜŰ]", stem):
            return False
        nxt = (following or "").lstrip("\"“„'(")
        if stem.isdigit() and nxt[:1].isalpha() and nxt[:1].islower():
            return False
    return True


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


_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U0001F1E6-\U0001F1FF\u2300-\u23FF\u2600-\u27BF\u2B00-\u2BFF"
                    "\uFE0E\uFE0F\u200D\u20E3]")


def no_emoji(text: str) -> str:
    """Post texts, titles and covers carry no emoji: many fonts draw them as empty boxes."""
    lines = (re.sub(r"[ \t]{2,}", " ", _EMOJI.sub("", line)).strip() for line in (text or "").split("\n"))
    return "\n".join(lines).strip()


def no_dashes(text: str) -> str:
    """Post and on-screen texts never use the long dashes typical of AI writing: a range gets a hyphen (20-45), a
    dash between words becomes a comma."""
    text = _RANGE_DASH.sub("-", text or "")
    text = _DASH.sub(", ", text).strip(" ,")
    return re.sub(r",\s*([,.!?…:;])", r"\1", text)


_STRAIGHT_QUOTES = re.compile(r'"([^"\n]{1,200})"')
_ENGLISH_QUOTES = re.compile(r"“([^”\n]{1,200})”")


HU_HINT = re.compile(
    r"[őűŐŰ]|\b(és|hogy|nem|igen|kérek|kérlek|csinálj|legyen|még|klipet|klipek|klip|ezt|azt|mert|vagy|"
    r"legyenek|szeretném|szeretnék|töröld|vágd|rövidebb|hosszabb|melyik|miért|hány|tedd|rakd|vedd|"
    r"zene|zenét|szöveg|szöveget|felirat|feliratot|kép|képet|színek|vége|elején|végén|alá|fel|le)\b",
    re.IGNORECASE)
HU_LETTERS = re.compile(r"[áéíóöúüÁÉÍÓÖÚÜ]")


def looks_hungarian(text: str) -> bool:
    """A post text written in Hungarian (it then gets the Hungarian call to comment and follow lines)."""
    return bool(HU_HINT.search(text or "")) or len(HU_LETTERS.findall(text or "")) >= 2


def hu_quotes(text: str) -> str:
    """Hungarian quotation marks: „…” instead of "…" or “…”."""
    return _ENGLISH_QUOTES.sub(r"„\1”", _STRAIGHT_QUOTES.sub(r"„\1”", text or ""))


def post_clean(text: str, language: str = "") -> str:
    text = no_dashes(clean_text(no_emoji(text)))
    return hu_quotes(text) if language in ("hu", "Hungarian") else text


def merge_suffixes(words: list[dict]) -> list[dict]:
    """Whisper splits Hungarian number suffixes ('10', '-ben'): glue them back into one caption token."""
    out: list[dict] = []
    for w in words:
        if out and w["word"].startswith("-") and len(w["word"]) > 1 and w["start"] - out[-1]["end"] < 0.3:
            out[-1] = {**out[-1], "word": out[-1]["word"] + w["word"], "end": w["end"]}
        else:
            out.append(dict(w))
    return out
