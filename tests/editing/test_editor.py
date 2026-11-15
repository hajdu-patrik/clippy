import json

from clippy.editing.editor import Editor
from clippy.editing.repository import TimelineRepository


def plan(catalogue, items):
    ops, errors = catalogue.parse(items)
    assert not errors, errors
    return ops


def test_a_full_request_becomes_one_new_version(timeline, ctx, catalogue):
    ops = plan(catalogue, [
        {"kind": "add_music", "asset": "a1b2c3", "gain_db": -16},
        {"kind": "set_outro", "asset": "aa11bb", "transition": "fade"},
        {"kind": "remove_layer", "layer": "hook"},
    ])
    result = Editor().run(timeline, ops, ctx)
    assert result.ok and result.changed and not result.touches_base
    assert result.timeline.version == timeline.version + 1
    assert [c.text("en") for c in result.changes] == [
        "Added music summer.mp3", "Added outro outro.mp4", "Removed hook “Elvesztettem magam – miért?”"]


def test_one_failing_operation_keeps_the_old_timeline_and_reports_why(timeline, ctx, catalogue):
    ops = plan(catalogue, [{"kind": "add_music", "asset": "a1b2c3"}, {"kind": "edit_text", "layer": "text-7", "size": 40}])
    result = Editor().run(timeline, ops, ctx)
    assert not result.ok and result.timeline == timeline
    assert result.issues[0].index == 1 and result.issues[0].text("hu") == "Nincs ilyen ezen a klipen: „text-7”."
    assert result.issues[0].for_model().startswith("#1 edit_text:")


def test_cuts_need_a_new_base_render_and_controls_are_passed_on(timeline, ctx, catalogue):
    ops = plan(catalogue, [{"kind": "cut_range", "span": {"start": {"type": "at", "at": 2}, "end": {"type": "at", "at": 4}}},
                           {"kind": "ask", "question": "Which text?"}])
    result = Editor().run(timeline, ops, ctx)
    assert result.touches_base and result.controls[0].question == "Which text?"


def test_catalogue_rejects_unknown_kinds_and_bad_fields_without_dropping_good_ones(catalogue):
    ops, errors = catalogue.parse([{"kind": "explode"}, {"kind": "color", "contrast": 9},
                                   {"kind": "color", "contrast": 1.2}, "junk"])
    assert len(ops) == 1 and ops[0].contrast == 1.2
    assert errors[0].startswith("#0: unknown kind 'explode'") and "contrast" in errors[1]
    assert "unknown kind None" in errors[2]


def test_schema_offers_every_operation_to_the_model(catalogue):
    schema = json.dumps(catalogue.schema())
    for kind in ("add_music", "add_text", "cut_range", "set_outro", "remove_layer", "ask", "undo"):
        assert f'"{kind}"' in schema


def test_repository_builds_missing_timelines_and_finds_the_undo_target(timeline):
    class DB:
        def clip_versions(self, chat_id, number):
            return [{"version": 1}, {"version": 2}, {"version": 3}]

    repo = TimelineRepository(DB())
    row = {"clip": {"start": 1, "end": 30, "hook": "H"}, "overrides": {"cta": "Follow"}, "version": 4, "timeline": None}
    built = repo.of(row, {"highlight": "red"})
    assert built.version == 4 and built.layer("captions").highlight == "red" and built.text_with_role("cta")
    assert repo.of({**row, "timeline": timeline.to_json()}) == timeline
    assert repo.undo_target(1, 1) == {"version": 2}
