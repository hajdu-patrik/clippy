import json
import re
from pathlib import Path

import pytest

from clippy.web import presets
from clippy.web.agent import run_agent

I18N_JS = Path(__file__).resolve().parents[1] / "clippy" / "web" / "static" / "js" / "i18n.js"
LINK = "https://youtu.be/abcdefghijk"


def templates() -> dict[tuple[str, str], str]:
    """The prompt texts the buttons put into the composer, per language, read from the UI's own catalogue."""
    text = I18N_JS.read_text(encoding="utf-8")
    start = text.index("hu:") if "hu:" in text else len(text) // 2
    found = {}
    for m in re.finditer(r'"prompt\.(\w+)": ("(?:[^"\\]|\\.)*")', text):
        found[("hu" if m.start() > start else "en", m.group(1))] = json.loads(m.group(2))
    return found


class SilentLLM:
    def chat_json(self, *args, **kwargs):
        return {"reply": "", "actions": []}


class ChattyLLM:
    """A model that reads the text loosely: wrong layout, its own lengths and count."""

    def chat_json(self, *args, **kwargs):
        return {"reply": "Ok", "actions": [{"type": "generate", "url": LINK, "n": 5, "instructions": "good parts",
                                            "options": {"layout": "auto", "min_len": 20, "max_len": 60}}]}


def test_every_button_has_a_prompt_in_both_languages():
    found = templates()
    for lang in ("hu", "en"):
        for preset in presets.PRESETS:
            text = found[(lang, preset.key)]
            assert presets.preset_of(text) is preset
            assert text.rstrip().endswith(("Videó:", "Video:"))


@pytest.mark.parametrize("model", [SilentLLM(), ChattyLLM()])
@pytest.mark.parametrize("lang", ["hu", "en"])
def test_a_preset_prompt_gives_the_same_settings_every_time(model, lang):
    expected = {"podcast": (6, 30, 60, "face", False), "gameplay": (5, 20, 45, "stream", False),
                "promo": (4, 15, 30, "face", True), "quick": (3, 10, 25, "auto", False)}
    for (text_lang, key), text in templates().items():
        if text_lang != lang:
            continue
        out = run_agent(model, {"id": 1}, [], [], text + LINK)
        [action] = [a for a in out["actions"] if a["type"] == "generate"]
        o = action["options"]
        assert (action["n"], o["min_len"], o["max_len"], o["layout"], o["hook"]) == expected[key], key
        assert action["url"] == LINK and presets.preset_of(text).instructions in action["instructions"]
        if key == "promo":
            assert o["cta"] == ("Részletek a bióban!" if lang == "hu" else "Details in bio!")
        else:
            assert not o.get("cta")


def test_edited_numbers_and_an_extra_wish_are_kept():
    text = templates()[("hu", "gameplay")].replace("5 klipet", "8 klipet").replace("20–45", "15–30")
    text = text.replace("Egyéb kérés: " + chr(10), "Egyéb kérés: csak a GTA-s részek, és kövesse az arcomat" + chr(10))
    action = presets.apply(text, [{"type": "generate", "url": LINK, "n": 5, "instructions": "",
                                   "options": {"layout": "face"}}])[0]
    assert action["n"] == 8 and (action["options"]["min_len"], action["options"]["max_len"]) == (15, 30)
    assert action["options"]["layout"] == "face" and "Also: csak a GTA-s részek" in action["instructions"]
    assert presets.apply("csinálj 3 klipet " + LINK, [{"type": "generate", "n": 3}]) == [{"type": "generate", "n": 3}]


def test_guesses_of_the_model_are_dropped_without_an_extra_wish():
    text = templates()[("hu", "podcast")] + LINK
    guess = {"type": "generate", "url": LINK, "n": 6, "instructions": "",
             "options": {"captions": "hu", "lang": "hu", "vocab": "név1 név2", "layout": "auto"}}
    [action] = presets.apply(text, [guess])
    assert not {"captions", "lang", "vocab"} & set(action["options"]) and action["options"]["layout"] == "face"
