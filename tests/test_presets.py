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
    expected = {"podcast": (4, 20, 45, "face", True), "gameplay": (5, 15, 60, "stream", False),
                "promo": (4, 15, 30, "face", True), "quick": (3, 10, 25, "auto", False),
                "edit": (1, 15, 90, "auto", False), "montage": (5, 5, 10, "auto", False)}
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
        assert ("then_edit" in action) == (key == "edit")


def test_edited_numbers_and_an_extra_wish_are_kept():
    text = templates()["gameplay"].replace("5 clips", "8 clips").replace("15-60", "15-30")
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


def test_an_extra_wish_does_not_let_the_model_add_an_opening_text_nobody_asked_for():
    base = templates()["edit"]
    plain = base.replace("Extra wishes: " + chr(10), "Extra wishes: my logo top right" + chr(10))
    guessed = [{"type": "generate", "url": LINK, "n": 1, "options": {"hook": True, "layout": "face"}}]
    action = presets.apply(plain, guessed)[0]
    assert action["options"]["hook"] is False and action["options"]["layout"] == "face"
    asked = base.replace("Extra wishes: " + chr(10), "Extra wishes: add an opening text" + chr(10))
    assert presets.apply(asked, guessed)[0]["options"]["hook"] is True


def test_a_montage_opens_strong_builds_up_and_ends_on_the_second_best():
    from clippy.web import montage

    rows = [{"number": n, "clip": {"score": s}} for n, s in ((1, 70), (2, 95), (3, 60), (4, 88), (5, 75))]
    assert [r["number"] for r in montage.order(rows)] == [2, 3, 1, 5, 4]
    base, ops = montage.joins(rows)
    assert base["number"] == 2 and [o.clip for o in ops] == [3, 1, 5, 4]
    assert all(o.transition_duration == montage.CUT for o in ops)
    assert montage.joins(rows[:1]) is None
    text = templates()["montage"]
    action = presets.apply(text + LINK, [{"type": "generate", "url": LINK, "n": 5, "options": {}}])[0]
    assert action["montage"] is True and action["n"] == 5 and action["options"]["max_len"] == 10
