import dataclasses

import pytest

from clippy.captions import TextEvent, build_ass, CaptionStyle
from clippy.editing.operations.motion import (AddBroll, AddEffect, AddSound, AddSticker, JoinClip, SetFormat,
                                              SetSpeed, UnjoinClip)
from clippy.editing.operations.structure import CutRange
from clippy.editing.operations.text import AddText
from clippy.editing.render import stickers
from clippy.editing.speed import SpeedMap, atempo_chain
from clippy.editing.timeline import SpeedRange, TimeRange
from clippy.editing.timeref import At, Span, Word
from clippy.tools import ClippyError


def texts(changes, lang="en"):
    return [c.text(lang) for c in changes]


def test_speed_map_places_every_moment_on_the_sped_up_clip():
    speed = SpeedMap.of((SpeedRange(factor=2.0), SpeedRange(span=TimeRange(start=4, end=6), factor=0.5)), 10.0)
    assert speed.segments == ((0.0, 4.0, 2.0), (4.0, 6.0, 0.5), (6.0, 10.0, 2.0))
    assert speed.duration == pytest.approx(2 + 4 + 2)
    assert speed.out(5.0) == pytest.approx(4.0) and speed.out(10.0) == pytest.approx(8.0)
    assert SpeedMap.of((), 10.0).identity and atempo_chain(0.25) == [0.5, 0.5]


def test_set_speed_replaces_overlapping_parts_and_resets(timeline, ctx):
    tl, changes = SetSpeed(factor=0.5, span=Span(start=At(at=2), end=At(at=4))).apply(timeline, ctx)
    tl, _ = SetSpeed(factor=2, span=Span(start=At(at=3), end=At(at=6))).apply(tl, ctx)
    assert [(r.span.start, r.span.end, r.factor) for r in tl.speed] == [(3, 6, 2)]
    assert texts(changes, "hu") == ["Módosítva: sebesség ×0.5"]
    tl, _ = SetSpeed(factor=1.5).apply(tl, ctx)
    tl, _ = SetSpeed(factor=1).apply(tl, ctx)
    assert tl.speed == ()
    with pytest.raises(ClippyError):
        SetSpeed(factor=4, span=None).apply(timeline, dataclasses.replace(ctx, resolver=type(ctx.resolver)(3.0, [])))


def test_format_effects_and_cut_transitions(timeline, ctx):
    tl, changes = SetFormat(format="16:9").apply(timeline, ctx)
    assert tl.size == (1920, 1080) and texts(changes) == ["Changed format 16:9"]
    tl, changes = AddEffect(effect="zoom", span=Span(start=Word(word="pénzt"))).apply(tl, ctx)
    assert tl.layer("effect-1").span == TimeRange(start=5.0, end=5.8)
    assert texts(changes, "hu") == ["Hozzáadva: ráközelítés"]
    with pytest.raises(ClippyError) as missing:
        AddEffect(effect="flash").apply(tl, ctx)
    assert missing.value.key == "edit.needs_moment"
    tl, _ = AddEffect(effect="black_white").apply(tl, ctx)
    assert tl.layer("effect-2").span is None
    tl, _ = CutRange(span=Span(start=At(at=8), end=At(at=9)), transition="flash").apply(tl, ctx)
    assert tl.layer("effect-3").effect == "flash" and tl.layer("effect-3").span == TimeRange(start=7.8, end=8.2)


def test_stickers_sounds_and_broll(timeline, ctx, monkeypatch):
    monkeypatch.setattr(stickers, "available", lambda: True)
    tl, changes = AddSticker(emoji="🔥", position="top-left", motion="bounce").apply(timeline, ctx)
    assert tl.layer("sticker-1").motion == "bounce" and texts(changes) == ["Added sticker 🔥"]
    tl, changes = AddSound(sound="whoosh", at=Word(word="pénzt")).apply(tl, ctx)
    assert tl.layer("sound-1").at == 5.0 and texts(changes, "hu") == ["Hozzáadva: suhanás hang"]
    tl, changes = AddSound(asset="a1b2c3", at=At(at=1)).apply(tl, ctx)
    assert tl.layer("sound-2").asset == "a1b2c3" and texts(changes) == ["Added sound effect summer.mp3"]
    with pytest.raises(ClippyError) as both:
        AddSound(sound="pop", asset="a1b2c3", at=At(at=1)).apply(tl, ctx)
    assert both.value.key == "edit.one_sound"
    tl, changes = AddBroll(asset="aa11bb", span=Span(start=At(at=2), length=3), full=False).apply(tl, ctx)
    layer = tl.layer("broll-1")
    assert layer.span == TimeRange(start=2, end=5) and not layer.full and layer.gain_db is None
    with pytest.raises(ClippyError):
        AddBroll(asset="d4e5f6").apply(tl, ctx)
    monkeypatch.setattr(stickers, "available", lambda: False)
    with pytest.raises(ClippyError) as no_font:
        AddSticker(emoji="🔥").apply(tl, ctx)
    assert no_font.value.key == "edit.no_emoji_font"


def test_joining_other_clips_of_the_chat(timeline, ctx):
    ctx = dataclasses.replace(ctx, number=1, clips={2: 3, 4: 1})
    tl, changes = JoinClip(clip=2, transition="slideleft").apply(timeline, ctx)
    tl, _ = JoinClip(clip=4).apply(tl, ctx)
    tl, _ = JoinClip(clip=2).apply(tl, ctx)
    assert [(j.clip, j.version, j.transition.kind) for j in tl.joins] == [(4, 1, "fade"), (2, 3, "fade")]
    assert texts(changes, "hu") == ["Hozzáadva: a(z) 2. klip a végére"]
    for bad, key in ((1, "edit.self_join"), (9, "edit.no_clip")):
        with pytest.raises(ClippyError) as err:
            JoinClip(clip=bad).apply(tl, ctx)
        assert err.value.key == key
    tl, changes = UnjoinClip(clip=4).apply(tl, ctx)
    assert [j.clip for j in tl.joins] == [2] and changes[0].action == "removed"


def test_the_planner_sees_every_new_layer(timeline, ctx, monkeypatch, asset_index):
    from clippy.editing.planner import EditRequest, PromptFormatter

    monkeypatch.setattr(stickers, "available", lambda: True)
    ctx = dataclasses.replace(ctx, number=1, clips={2: 1})
    tl = timeline
    for op in (AddSticker(emoji="😂"), AddEffect(effect="blur"), AddSound(sound="ding", at=At(at=2)),
               AddBroll(asset="aa11bb"), SetSpeed(factor=2), SetFormat(format="1:1"), JoinClip(clip=2)):
        tl, _ = op.apply(tl, ctx)
    state = PromptFormatter().state(EditRequest("x", tl, tuple(asset_index.assets.values()), others=(2,)))
    for line in ("sticker 😂 (pop)", "effect blur whole clip", "sound effect ding at 2.0 s",
                 'B-roll "outro.mp4" full frame', "Format: 1:1. Speed: ×2 whole clip. Joined after it: clip #2. "
                 "Other clips in this chat: #2."):
        assert line in state


def test_text_animations_become_ass_tags(timeline, ctx):
    tl, _ = AddText(text="Egy kettő három", animation="typewriter").apply(timeline, ctx)
    assert tl.layer("text-1").style.animation == "typewriter"
    event = TextEvent(text="Egy kettő három", start=1.0, end=5.0, x=540, y=400, animation="typewriter")
    ass = build_ass([], 0, 6, CaptionStyle(), texts=(event,), frame=(1920, 1080))
    lines = [x for x in ass.splitlines() if x.startswith("Dialogue")]
    assert "PlayResX: 1920" in ass and [x.rsplit("}", 1)[1] for x in lines] == ["Egy", "Egy kettő", "Egy kettő három"]
    pop = build_ass([], 0, 6, CaptionStyle(), texts=(dataclasses.replace(event, animation="pop"),))
    assert r"\t(0,140,\fscx112\fscy112)" in pop
    slide = build_ass([], 0, 6, CaptionStyle(), texts=(dataclasses.replace(event, animation="slide"),))
    assert r"\move(540,560,540,400,0,300)" in slide
