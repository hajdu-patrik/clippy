import pytest

from clippy.editing.operations.audio import AddMusic, AdjustAudio
from clippy.editing.operations.meta import RemoveLayer
from clippy.editing.operations.structure import CutRange, SetCamera, SetIntro, SetOutro, Trim
from clippy.editing.operations.text import AddText, EditText, FixCaption, SetCta, SetHook, StyleCaptions
from clippy.editing.operations.visual import AddOverlay, ApplyLut, Color
from clippy.editing.timeline import TimeRange
from clippy.editing.timeref import At, Edge, Span, Word, corrected
from clippy.tools import ClippyError


def texts(changes):
    return [c.text() for c in changes]


def test_add_and_edit_text(timeline, ctx):
    tl, changes = AddText(text="Part 2 tomorrow", position="bottom", span=Span(start=Edge(edge="end", offset=-3)),
                          color="#FFD000").apply(timeline, ctx)
    layer = tl.layer("text-1")
    assert layer.text == "Part 2 tomorrow" and layer.span == TimeRange(start=17, end=20)
    assert layer.style.color == "#FFD000" and layer.transform.y == 0.82
    assert texts(changes) == ["Added text “Part 2 tomorrow”"]
    tl, changes = EditText(layer="text-1", text="Holnap jön", size=90).apply(tl, ctx)
    assert tl.layer("text-1").text == "Holnap jön" and tl.layer("text-1").style.size == 90
    assert texts(changes) == ["Changed text “Holnap jön”"]


def test_hook_and_cta_replace_their_role_layers(timeline, ctx):
    tl, changes = SetHook(text="Ezt senki nem mondta").apply(timeline, ctx)
    assert tl.text_with_role("hook").text == "Ezt senki nem mondta" and len(tl.layers_of("text")) == 2
    assert changes[0].action == "changed"
    bare = timeline.without_layer("cta")
    tl, changes = SetCta(text="Follow!").apply(bare, ctx)
    assert tl.text_with_role("cta").transform.y == 0.82 and changes[0].action == "added"


def test_the_opening_text_moves_without_new_text(timeline, ctx):
    tl, _ = SetHook(text="GTA 6 is out TOMORROW").apply(timeline, ctx)
    tl, changes = SetHook(position="center").apply(tl, ctx)
    hook = tl.text_with_role("hook")
    assert (hook.text, hook.transform.y, changes[0].action) == ("GTA 6 is out TOMORROW", 0.5, "changed")
    with pytest.raises(ClippyError):
        SetCta(position="top").apply(timeline.without_layer("cta"), ctx)


def test_caption_style_and_hiding(timeline, ctx):
    tl, _ = StyleCaptions(highlight="cyan", height="middle", uppercase=False).apply(timeline, ctx)
    captions = tl.layer("captions")
    assert (captions.highlight, captions.position, captions.uppercase) == ("cyan", 0.5, False)
    tl, changes = StyleCaptions(visible=False).apply(tl, ctx)
    assert tl.layer("captions").visible is False and changes[0].action == "removed"


def test_overlay_needs_an_image_and_gets_its_own_id(timeline, ctx):
    tl, changes = AddOverlay(asset="d4e5f6", position="top-right", width=0.2, opacity=0.7).apply(timeline, ctx)
    logo = tl.layer("image-1")
    assert logo.transform.x == 0.8 and logo.transform.opacity == 0.7 and texts(changes) == ["Added image logo.png"]
    with pytest.raises(ClippyError) as wrong:
        AddOverlay(asset="a1b2c3").apply(timeline, ctx)
    assert wrong.value.key == "edit.wrong_asset"


def test_colour_and_lut_share_one_colour_layer(timeline, ctx):
    tl, _ = Color(contrast=1.15, temperature=5600).apply(timeline, ctx)
    tl, changes = ApplyLut(asset="cc22dd", strength=0.5).apply(tl, ctx)
    color = tl.layer("color")
    assert len(tl.layers_of("color")) == 1 and color.adjust.contrast == 1.15 and color.lut == "cc22dd"
    assert texts(changes) == ["Added colour look warm.cube"]


def test_music_levels_and_speech_volume(timeline, ctx):
    tl, changes = AddMusic(asset="a1b2c3", gain_db=-18, span=Span(start=Word(word="pénzt"))).apply(timeline, ctx)
    music = tl.layer("music-1")
    assert music.gain_db == -18 and music.duck and music.span.start == 5.0
    tl, _ = AdjustAudio(layer="music-1", gain_db=-24, fade_out=4).apply(tl, ctx)
    assert tl.layer("music-1").gain_db == -24 and tl.layer("music-1").fade_out == 4
    tl, changes = AdjustAudio(layer="speech", gain_db=3).apply(tl, ctx)
    assert tl.audio.speech_gain_db == 3 and texts(changes) == ["Changed speech volume (+3 dB)"]
    with pytest.raises(ClippyError):
        AdjustAudio(layer="music-9", gain_db=-3).apply(tl, ctx)


def test_cut_range_maps_to_source_time_and_merges(timeline, ctx):
    assert CutRange.touches_base
    tl, changes = CutRange(span=Span(start=At(at=3), end=At(at=11))).apply(timeline, ctx)
    assert tl.cut.removed == (TimeRange(start=103, end=113),)
    tl, _ = CutRange(span=Span(start=At(at=14), end=At(at=16))).apply(tl, ctx)
    assert tl.cut.removed == (TimeRange(start=103, end=113), TimeRange(start=116, end=118))
    tl, _ = CutRange(span=Span(start=At(at=11), end=At(at=12))).apply(tl, ctx)
    assert tl.cut.removed == (TimeRange(start=103, end=114), TimeRange(start=116, end=118))
    assert texts(changes) == ["Removed the part 3.0-11.0 s"]
    with pytest.raises(ClippyError) as short:
        CutRange(span=Span(start=At(at=1), end=At(at=19))).apply(timeline, ctx)
    assert short.value.key == "edit.too_short"


def test_a_cut_in_seconds_never_splits_a_sentence(timeline, ctx):
    tl, changes = CutRange(span=Span(start=At(at=6), end=At(at=7))).apply(timeline, ctx)
    [removed] = tl.cut.removed
    assert (removed.start, removed.end) == (106.56, 107.0)
    assert texts(changes) == ["Removed the part 6.6-7.0 s"], "the sentence ends at 6.5 s: it stays whole"
    tl, changes = CutRange(span=Span(start=At(at=5.2), end=At(at=5.6))).apply(timeline, ctx)
    assert texts(changes) == ["Removed the part 4.8-6.6 s"], "inside one sentence: that sentence goes whole"
    tl, changes = CutRange(span=Span(start=Word(word="keresni"), end=At(at=7))).apply(timeline, ctx)
    assert texts(changes) == ["Removed the part 5.6-7.0 s"], "a word edge stays exact"


def test_trim_camera_and_bumpers(timeline, ctx):
    tl, _ = Trim(start_shift=-2, end_shift=3).apply(timeline, ctx)
    assert tl.cut.source == TimeRange(start=98, end=123)
    with pytest.raises(ClippyError):
        Trim(start_shift=19).apply(timeline, ctx)
    tl, _ = SetCamera(layout="blur", punch=False).apply(tl, ctx)
    assert (tl.camera.layout, tl.camera.punch) == ("blur", False)
    tl, changes = SetOutro(asset="aa11bb", transition="slideup").apply(tl, ctx)
    assert tl.outro.transition.kind == "slideup" and tl.outro.duration is None
    tl, _ = SetIntro(asset="d4e5f6").apply(tl, ctx)
    assert tl.intro.duration == 3.0
    tl, changes = RemoveLayer(layer="outro").apply(tl, ctx)
    assert tl.outro is None and texts(changes) == ["Removed outro outro.mp4"]


def test_remove_layer_describes_what_went(timeline, ctx):
    tl, changes = RemoveLayer(layer="hook").apply(timeline, ctx)
    assert tl.text_with_role("hook") is None
    assert texts(changes) == ["Removed opening text “Elvesztettem magam – miért?”"]
    with pytest.raises(ClippyError):
        RemoveLayer(layer="nothing").apply(timeline, ctx)


def test_a_misheard_word_is_fixed_in_the_captions(timeline, ctx, words):
    tl, changes = FixCaption(find="megállítani", replace="meg állítani").apply(timeline, ctx)
    tl, _ = FixCaption(find="pénzt keresni", replace="Pénzt").apply(tl, ctx)
    tl, _ = FixCaption(find="Megállítani", replace="leállítani").apply(tl, ctx)
    fixes = tl.layer("captions").fixes
    assert [(f.find, f.replace) for f in fixes] == [("pénzt keresni", "Pénzt"), ("Megállítani", "leállítani")]
    assert texts(changes) == ["Changed captions: “megállítani” → “meg állítani”"]
    fixed = corrected(words, fixes)
    assert [w["word"] for w in fixed][:5] == ["Senki", "nem", "tud", "leállítani.", "Pénzt"]
    assert (fixed[4]["start"], fixed[4]["end"]) == (5.0, 6.0) and len(fixed) == len(words) - 1
    assert [w["word"] for w in words][3] == "megállítani."
    with pytest.raises(ClippyError) as missing:
        FixCaption(find="Pistának", replace="Petinek").apply(timeline, ctx)
    assert missing.value.key == "edit.no_word"


def test_a_fix_with_more_words_shares_the_time():
    fixed = corrected([{"start": 1.0, "end": 2.0, "word": "féleltem."}],
                      [FixCaption(find="féleltem", replace="fél arcom")])
    assert [(w["word"], w["start"], w["end"]) for w in fixed] == [("fél", 1.0, 1.333), ("arcom.", 1.333, 2.0)]
