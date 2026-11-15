import pytest
from pydantic import ValidationError

from clippy.editing.timeline import ColorLayer, TextLayer, TimeRange, Timeline


def test_factory_turns_an_old_clip_into_layers(timeline):
    assert [layer.id for layer in timeline.layers] == ["captions", "hook", "cta"]
    assert timeline.layer("captions").highlight == "green"
    assert timeline.text_with_role("hook").text == "Elvesztettem magam – miért?"
    assert timeline.text_with_role("cta").text == "Kövess!"
    assert timeline.camera.layout == "face" and timeline.cut.source == TimeRange(start=100, end=120)


def test_timelines_are_immutable_and_validated(timeline):
    with pytest.raises(ValidationError):
        timeline.version = 5
    with pytest.raises(ValidationError):
        timeline.with_layer(TextLayer(id="hook", text="duplicate id"))
    with pytest.raises(ValidationError):
        TimeRange(start=5, end=2)


def test_keys_separate_base_work_from_finishing_work(timeline):
    restyled = timeline.with_layer(ColorLayer(id="color"))
    assert restyled.base_key() == timeline.base_key() and restyled.finish_key() != timeline.finish_key()
    recut = timeline.evolve(cut=timeline.cut.evolve(source=TimeRange(start=101, end=120)))
    assert recut.base_key() != timeline.base_key()
    assert timeline.next_version().finish_key() == timeline.finish_key()


def test_json_round_trip_keeps_every_layer_type(timeline):
    full = timeline.with_layer(ColorLayer(id="color", lut="cc22dd", strength=0.6))
    assert Timeline.from_json(full.to_json()) == full


def test_a_new_clip_is_only_the_moment_with_captions():
    from clippy.editing.factory import timeline_from_clip
    from clippy.pipeline import Options

    plain = timeline_from_clip({"start": 1, "end": 30, "hook": "Erre senki nem számított"}, {})
    assert [layer.id for layer in plain.layers] == ["captions"] and not plain.camera.punch
    assert not Options().hook and not Options().punch and Options().cta == ""
