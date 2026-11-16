import json

import pytest

from clippy.editing.planner import EditPlanner, EditRequest, PromptFormatter, verbatim
from clippy.editing.render.base import BaseRender, BaseStore
from clippy.editing.service import EditService


class ScriptedLLM:
    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def chat_json(self, system, user, think=False, schema=None):
        self.prompts.append((system, user, schema))
        return self.replies.pop(0)


@pytest.fixture
def make_request(timeline, words, asset_index):
    def make(text="Tedd alá a zenét halkan", attached=("a1b2c3",)):
        return EditRequest(text, timeline, tuple(asset_index.assets.values()), attached, tuple(words), 2, "Old title")
    return make


def test_prompt_lists_layers_attachments_and_the_transcript(make_request):
    state = PromptFormatter().state(make_request())
    assert 'hook: hook text "Elvesztettem magam – miért?" (first 3 s)' in state
    assert '- a1b2c3 audio "summer.mp3" 161 s  [attached to this message]' in state
    assert "[0.0] Senki nem tud megállítani." in state and "CLIP #2, version 1" in state


def test_valid_plan_is_applied_in_one_call(make_request, ctx):
    llm = ScriptedLLM({"reply": "Kész.", "operations": [{"kind": "add_music", "asset": "a1b2c3", "gain_db": -20}]})
    plan = EditPlanner(llm).plan(make_request(), ctx)
    assert plan.reply == "Kész." and plan.result.ok and plan.result.timeline.layer("music-1")
    system, _, schema = llm.prompts[0]
    assert "in English" in system and "- add_music(asset: text" in system
    assert schema["properties"]["operations"]["maxItems"] == 10


def test_failures_go_back_to_the_model_once(make_request, ctx):
    llm = ScriptedLLM(
        {"reply": "x", "operations": [{"kind": "remove_layer", "layer": "title"}, {"kind": "color", "contrast": 9}]},
        {"reply": "Levettem a hookot.", "operations": [{"kind": "remove_layer", "layer": "hook"}]})
    plan = EditPlanner(llm).plan(make_request("vedd le a címet"), ctx)
    assert plan.result.ok and plan.result.timeline.text_with_role("hook") is None
    retry = llm.prompts[1][1]
    assert "THEY FAILED" in retry and "contrast" in retry and "There is no “title”" in retry


def test_ambiguous_requests_come_back_as_a_question(make_request, ctx):
    llm = ScriptedLLM({"reply": "Melyik szöveget?", "operations": [{"kind": "ask", "question": "Melyik szöveget?"}]})
    plan = EditPlanner(llm).plan(make_request("vedd le a szöveget"), ctx)
    assert plan.control("ask").question == "Melyik szöveget?" and not plan.result.changed


def test_service_asks_back_and_undoes_without_rendering(timeline, words, asset_index, tmp_path):
    from clippy.storage import Storage

    storage = Storage(tmp_path / "data", tmp_path / "library")
    base = BaseRender(tmp_path / "b.mkv", 20.0, 25.0, "25", words, ((100.0, 120.0),), {})
    (tmp_path / "b.mkv").write_bytes(b"x")
    BaseStore(storage.chat_base(5)).put(timeline.base_key(), base)
    rows = [{"number": 2, "version": 1, "clip": {"start": 100, "end": 120, "title": "T"}, "info": {}, "timeline": None,
             "mp4": "v1.mp4", "jpg": "v1.jpg", "txt": "v1.txt", "overrides": {}},
            {"number": 2, "version": 2, "clip": {"start": 100, "end": 120}, "info": {}, "timeline": timeline.to_json(),
             "mp4": "v2.mp4", "jpg": "v2.jpg", "txt": "v2.txt", "overrides": {}}]

    class DB:
        def clip_versions(self, chat_id, number):
            return rows

        def current_clips(self, chat_id):
            return rows[-1:]

    class Assets:
        def index(self, chat_id):
            return asset_index

    class Project:
        source, transcript, scratch_root = "src", {"duration": 600}, None

    Project.storage = storage
    chat = {"id": 5, "options": {}}
    asking = EditService(DB(), storage, Assets(), EditPlanner(ScriptedLLM(
        {"reply": "Which?", "operations": [{"kind": "ask", "question": "Which text?"}]})))
    outcome = asking.edit(Project(), chat, {**rows[1], "timeline": timeline.to_json()}, "remove the text")
    assert outcome.question == "Which text?" and outcome.version is None
    undoing = EditService(DB(), storage, Assets(), EditPlanner(ScriptedLLM(
        {"reply": "Visszaállítottam.", "operations": [{"kind": "undo"}]})))
    outcome = undoing.edit(Project(), chat, {**rows[1], "timeline": timeline.to_json()}, "vond vissza")
    assert outcome.version.mp4 == "v1.mp4" and outcome.changes[0].text() == "Changed back to version 1"
    assert json.loads(outcome.version.timeline)["cut"]["source"]["start"] == 100


def test_repeated_operations_are_dropped(make_request, ctx):
    loop = [{"kind": "style_captions", "highlight": "cyan"}] * 6
    plan = EditPlanner(ScriptedLLM({"reply": "ok", "operations": loop})).plan(make_request("cyan"), ctx)
    assert len(plan.operations) == 1 and plan.result.ok


def test_quoted_texts_go_on_the_video_letter_for_letter():
    request = 'A hook legyen: "Fogorvosnál lebénult a fél arcom", a felirat: „féleltem” helyett „fél arcom”'
    items = [{"kind": "set_hook", "text": "Fogorvosnál lebánult a fél arcom"},
             {"kind": "fix_caption", "find": "féleltem", "replace": "fél arcom"},
             {"kind": "add_text", "text": "Kövess!"}]
    assert verbatim(items, request) == [{"kind": "set_hook", "text": "Fogorvosnál lebénult a fél arcom"},
                                        items[1], items[2]]
    assert verbatim(items, "no quotes here") == items
