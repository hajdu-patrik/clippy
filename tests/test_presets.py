import json
import re
from pathlib import Path

import pytest

from clippy.web import presets
from clippy.web.agent import run_agent

I18N_JS = Path(__file__).resolve().parents[1] / "clippy" / "web" / "static" / "js" / "i18n.js"
LINK = "https://youtu.be/abcdefghijk"


def templates() -> dict[str, str]:
    """The prompt texts the buttons put into the composer, read from the UI's own catalogue."""
    text = I18N_JS.read_text(encoding="utf-8")
    return {m.group(1): json.loads(m.group(2)) for m in re.finditer(r'"prompt\.(\w+)": ("(?:[^"\\]|\\.)*")', text)}


class SilentLLM:
    def chat_json(self, *args, **kwargs):
        return {"reply": "", "actions": []}


class ChattyLLM:
    """A model that reads the text loosely: wrong layout, its own lengths and count."""

    def chat_json(self, *args, **kwargs):
        return {"reply": "Ok", "actions": [{"type": "generate", "url": LINK, "n": 5, "instructions": "good parts",
                                            "options": {"layout": "auto", "min_len": 20, "max_len": 60}}]}


def test_every_button_has_a_prompt():
    found = templates()
    for preset in presets.PRESETS:
        text = found[preset.key]
        assert presets.preset_of(text) is preset and text.rstrip().endswith("Video:")


@pytest.mark.parametrize("model", [SilentLLM(), ChattyLLM()])
def test_a_preset_prompt_gives_the_same_settings_every_time(model):
    expected = {"podcast": (6, 30, 90, "face", False), "gameplay": (5, 20, 75, "stream", False),
                "promo": (4, 15, 30, "face", True), "quick": (3, 10, 25, "auto", False)}
    for key, text in templates().items():
        out = run_agent(model, {"id": 1}, [], [], text + LINK)
        [action] = [a for a in out["actions"] if a["type"] == "generate"]
        o = action["options"]
        assert (action["n"], o["min_len"], o["max_len"], o["layout"], o["hook"]) == expected[key], key
        assert action["url"] == LINK and presets.preset_of(text).instructions in action["instructions"]
        if key == "promo":
            assert o["cta"] == "Details in bio!"
        else:
            assert not o.get("cta")


def test_edited_numbers_and_an_extra_wish_are_kept():
    text = templates()["gameplay"].replace("5 clips", "8 clips").replace("20-75", "15-30")
    text = text.replace("Extra wishes: " + chr(10), "Extra wishes: only the GTA parts, and follow my face" + chr(10))
    action = presets.apply(text, [{"type": "generate", "url": LINK, "n": 5, "instructions": "",
                                   "options": {"layout": "face"}}])[0]
    assert action["n"] == 8 and (action["options"]["min_len"], action["options"]["max_len"]) == (15, 30)
    assert action["options"]["layout"] == "face" and "Also: only the GTA parts" in action["instructions"]
    assert presets.apply("csinálj 3 klipet " + LINK, [{"type": "generate", "n": 3}]) == [{"type": "generate", "n": 3}]


def test_guesses_of_the_model_are_dropped_without_an_extra_wish():
    text = templates()["podcast"] + LINK
    guess = {"type": "generate", "url": LINK, "n": 6, "instructions": "",
             "options": {"captions": "hu", "lang": "hu", "vocab": "név1 név2", "layout": "auto"}}
    [action] = presets.apply(text, [guess])
    assert not {"captions", "lang", "vocab"} & set(action["options"]) and action["options"]["layout"] == "face"
