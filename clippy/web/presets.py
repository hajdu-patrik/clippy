"""Ready-made prompts (the buttons on the start screen): the text the user sends names a preset, and the preset makes
the result the same every time: its settings and picking rules reach the clip picker whatever the chat model makes
of the text. The count, the lengths and the CTA are read from the text, so editing those works."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

COUNT = re.compile(r"\b(\d{1,2})\s*clips?", re.I)
LENGTHS = re.compile(r"\b(\d{1,3})\s*[–-]\s*(\d{1,3})\s*(?:seconds?\b|s\b)", re.I)
EXTRA = re.compile(r"^[ \t]*Extra wishes[ \t]*:[ \t]*(\S.*?)[ \t]*$", re.I | re.M)
CTA = re.compile(r"CTA[^:\n]{0,20}:\s*[„“\"]([^”\"\n]{1,80})[”\"]")
MAX_INSTRUCTIONS = 900


@dataclass(frozen=True)
class Preset:
    key: str
    marker: str
    options: dict = field(default_factory=dict)
    instructions: str = ""


PRESETS = (
    Preset("podcast", "🎙",
           {"n": 6, "min_len": 30, "max_len": 60, "layout": "face", "audio": "voice", "tighten": True,
            "hook": False, "punch": False},
           "Podcast or interview. Pick standalone moments a new viewer understands without the rest: a strong or "
           "debatable opinion, a surprising fact or number, a personal story with its payoff, funny banter, an honest "
           "or emotional confession. Start on the strongest sentence (no greeting, set-up or filler), one idea per "
           "clip, stop right after the payoff."),
    Preset("gameplay", "🎮",
           {"n": 5, "min_len": 20, "max_len": 45, "layout": "stream", "audio": "natural", "tighten": False,
            "hook": False, "punch": False},
           "Gameplay or stream. Pick the peaks: clutch or skilful plays, wins, big fails, unexpected twists, the "
           "streamer's loud reactions, laughter, rage, funny lines, jokes with chat or teammates. Start 1-3 s before "
           "the peak, stop right after the reaction; skip loading, menus and idle talk."),
    Preset("promo", "📣",
           {"n": 4, "min_len": 15, "max_len": 30, "layout": "face", "audio": "voice", "tighten": True,
            "hook": True, "punch": False},
           "Promotion of a product, service, channel or event. Pick the moments that say most clearly what the "
           "viewer gets: the main benefit or result, a demonstration, a strong claim or number, a testimonial. "
           "Hook, then proof, then the call to action; the point lands in the first 2 seconds; end on a complete "
           "thought."),
    Preset("quick", "⚡",
           {"n": 3, "min_len": 10, "max_len": 25, "layout": "auto", "tighten": True, "hook": False, "punch": False},
           "Quick shorts. Pick the punchiest moments that work alone: a joke, a punchline, a surprising line or "
           "reaction. Start on the point and end right after the payoff."),
)


def preset_of(message: str) -> Preset | None:
    """The preset a message was written from: it starts with the preset's emoji."""
    text = (message or "").lstrip()
    return next((p for p in PRESETS if text.startswith(p.marker)), None)


def apply(message: str, actions: list[dict]) -> list[dict]:
    """New-clip actions get the preset's settings and picking rules. Settings the model read from the text count only
    when the user wrote an extra wish (that is where a change of style goes); otherwise a small model's guesses
    (a caption language from the message's language, made-up names to spell) would make runs differ."""
    preset = preset_of(message)
    if preset is None:
        return actions
    extra = EXTRA.search(message)
    count, lengths, cta = COUNT.search(message), LENGTHS.search(message), CTA.search(message)
    out = []
    for action in actions:
        if action.get("type") in ("generate", "more"):
            fixed = {k: v for k, v in preset.options.items() if k != "n"}
            given = action.get("options") or {}
            options = {**fixed, **given} if extra else fixed
            if lengths:
                low, high = sorted(float(x) for x in lengths.groups())
                options["min_len"], options["max_len"] = min(max(low, 8.0), 120.0), min(max(high, 10.0), 180.0)
            if cta:
                options["cta"] = cta.group(1).strip()
            parts = [preset.instructions, f"Also: {extra.group(1)}" if extra else "",
                     str(action.get("instructions") or "")]
            action = {**action, "n": max(1, min(12, int(count.group(1)))) if count else preset.options["n"],
                      "options": options, "instructions": "\n".join(p for p in parts if p)[:MAX_INSTRUCTIONS]}
        out.append(action)
    return out
