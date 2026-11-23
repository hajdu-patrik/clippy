import math
from pathlib import Path

import numpy as np
import pytest

from clippy import audio as aud
from clippy.analysis import ClipAnalysis, FaceObs
from clippy.captions import CaptionStyle, build_ass, group_words
from clippy.director import _camera, plan_frames
from clippy.edit import build_edit
from clippy.highlights import Clip, _snap, _windows, sentences
from clippy.llm import parse_json_reply
from clippy.text import clean_text, ends_clause, ends_sentence, is_filler, merge_suffixes
from clippy.compositing import CropCompositor, FitCompositor, cover
from clippy.director import FramePlan
from clippy.tools import ClippyError

SAMPLE = Path(__file__).parent / "sample" / "sample.mp4"


def words_from(text: str, start: float = 0.0, step: float = 0.4, gap_after: dict | None = None):
    out, t = [], start
    for i, w in enumerate(text.split()):
        out.append({"start": round(t, 3), "end": round(t + step - 0.05, 3), "word": w})
        t += step + (gap_after or {}).get(i, 0.0)
    return out


def test_group_words_breaks_on_punctuation_and_size():
    groups = group_words(words_from("Ez egy nagyon jó mondat. Utána jön a következő rész"), 3, 18)
    texts = [" ".join(w["word"] for w in g) for g in groups]
    assert texts[1].endswith("mondat.")
    assert all(len(g) <= 3 for g in groups)


def test_ass_relative_times_hook_cta_and_lead():
    ws = words_from("one two three four five six", start=100.0)
    ass = build_ass(ws, 100.0, 103.0, CaptionStyle(), hook="Hook text", cta="Follow!")
    assert "Hook text" in ass and "Follow!" in ass and "ONE" in ass
    first = [l for l in ass.splitlines() if l.startswith("Dialogue: 0,")][0]
    assert first.startswith("Dialogue: 0,0:00:00.00")


def test_captions_do_not_blink_on_short_pauses():
    ws = words_from("egy kettő három. négy öt hat", step=0.4)
    ass = build_ass(ws, 0.0, 3.0, CaptionStyle())
    lines = [l for l in ass.splitlines() if l.startswith("Dialogue: 0,")]
    ends = [l.split(",")[2] for l in lines]
    starts = [l.split(",")[1] for l in lines]
    assert ends[2] == starts[3]


def test_parse_json_reply_tolerates_think_and_fences():
    reply = '<think>hmm</think>\n```json\n{"clips": [{"start": 1, "end": 30}]}\n```'
    assert parse_json_reply(reply)["clips"][0]["end"] == 30


def test_a_model_that_times_out_or_answers_garbage_fails_as_an_llm_error(monkeypatch):
    import requests

    from clippy import llm

    def slow(*args, **kwargs):
        raise requests.ReadTimeout("read timed out")

    monkeypatch.setattr(llm.requests, "post", slow)
    with pytest.raises(ClippyError) as caught:
        llm.OllamaLLM("m", "http://127.0.0.1:9").chat_json("system", "user")
    assert caught.value.key == "error.llm_failed"

    class Garbage:
        status_code, ok, text = 200, True, "<html>"

        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(llm.requests, "post", lambda *args, **kwargs: Garbage())
    with pytest.raises(ClippyError) as caught:
        llm.OpenAICompatibleLLM("m", "http://api.example").chat_json("system", "user")
    assert caught.value.key == "error.llm_failed"


def test_sentences_split_on_punctuation():
    s = sentences(words_from("Első mondat. Második mondat itt! Harmadik?"))
    assert [x["text"] for x in s] == ["Első mondat.", "Második mondat itt!", "Harmadik?"]


def test_snap_never_swallows_neighbour_words():
    ws = words_from("Előző szó. " + "Ez a klip mondata ami szépen tart egészen idáig. " * 3 + "Következő")
    clip = _snap(Clip(1.0, 9.0, 80), ws, min_len=5, max_len=30)
    i0 = next(i for i, w in enumerate(ws) if w["start"] >= clip.start)
    assert clip.start >= ws[i0 - 1]["end"]
    nxt = next(w for w in ws if w["start"] > clip.end - 0.5 and w["start"] >= clip.end)
    assert clip.end <= nxt["start"]


def test_windows_overlap_and_cover():
    segs = [{"start": i * 10.0, "end": i * 10.0 + 9, "text": "x"} for i in range(100)]
    wins = _windows(segs, 300, 60)
    assert wins[0][0]["start"] == 0 and wins[-1][-1]["start"] == 990 and len(wins) == 4


def test_fillers():
    assert is_filler("Ööö,") and is_filler("hmm") and not is_filler("ő")


def test_build_edit_cuts_quiet_gaps_keeps_loud_gaps_and_stays_on_frame_grid():
    fps = 25.0
    ws = words_from("a b c d e f", step=0.4, gap_after={1: 1.2, 3: 1.2})
    span = ws[-1]["end"] + 0.4
    env = np.full(int(span / 0.01) + 1, -25.0)
    g1 = (ws[1]["end"], ws[2]["start"])
    for i in range(int(g1[0] / 0.01) + 2, int(g1[1] / 0.01) - 2):
        env[i] = -70.0
    ep = build_edit(ws, 0.0, span, fps, env=env)
    assert len(ep.intervals) == 2
    assert ep.removed > 0.8
    for (a, b), (ka, kb) in zip(ep.intervals, ep.frames):
        assert math.isclose(a * fps, ka) and math.isclose(b * fps, kb)
    assert math.isclose(ep.duration, sum(b - a for a, b in ep.intervals))
    starts = [w["start"] for w in ep.words]
    assert starts == sorted(starts) and starts[-1] < ep.duration
    assert ep.join_frames == [ep.frames[1][0]]


def test_build_edit_without_tighten_is_one_interval_and_drops_fillers():
    ws = words_from("szia öö hogy vagy")
    ep = build_edit(ws, 0.0, 2.0, 30.0, tighten=False)
    assert len(ep.intervals) == 1
    assert [w["word"] for w in ep.words] == ["szia", "hogy", "vagy"]


def test_camera_ignores_jitter_and_moves_smoothly():
    n = 100
    tx = np.array([0.5 + 0.005 * math.sin(i) for i in range(n)])
    ty = np.full(n, 0.4)
    cams = _camera(tx, ty, set(), 25.0, dz=0.03, cut_dist=0.1)
    assert max(abs(c[0] - 0.5) for c in cams) < 0.006
    tx2 = np.concatenate([np.full(50, 0.3), np.full(50, 0.6)])
    cams2 = _camera(tx2, ty, set(), 25.0, dz=0.03, cut_dist=10)
    steps = np.abs(np.diff([c[0] for c in cams2]))
    assert steps.max() < 0.05 and abs(cams2[-1][0] - 0.6) < 0.01


def _fake_analysis(n=100, faces_fn=None, cuts=(0,)):
    faces = [faces_fn(i) if faces_fn else [] for i in range(n)]
    return ClipAnalysis(25.0, n, 1920, 1080, faces, list(cuts), "test")


def test_a_facecam_is_a_face_that_stays_put():
    from clippy.director import guess_facecam

    still = _fake_analysis(100, lambda i: [FaceObs(0.82, 0.1, 0.05, 0.09, 0.9, 0.12)])
    x, y, w, h = guess_facecam(still)
    assert x < 0.82 < x + w and y < 0.12 < y + h
    roaming = _fake_analysis(100, lambda i: [FaceObs(0.1 + 0.007 * i, 0.3 + 0.002 * i, 0.05, 0.09, 0.9, 0.35)])
    assert guess_facecam(roaming) is None
    close_up = _fake_analysis(100, lambda i: [FaceObs(0.35, 0.2, 0.3, 0.5, 0.9, 0.35)])
    assert guess_facecam(close_up) is None
    crowded = _fake_analysis(100, lambda i: [FaceObs(0.04, 0.8, 0.05, 0.09, 0.9, 0.82)] + [
        FaceObs(0.3 + 0.004 * i + 0.1 * k, 0.3, 0.06, 0.1, 0.9, 0.33) for k in range(3)])
    x, y, w, h = guess_facecam(crowded)
    assert x < 0.065 < x + w and y < 0.845 < y + h


def test_plan_switches_to_active_speaker_with_hard_cut():
    def faces(i):
        left = FaceObs(0.15, 0.3, 0.1, 0.18, 0.9, 0.35, track=1, mouth=0.03 if i < 50 else 0.0)
        right = FaceObs(0.75, 0.3, 0.1, 0.18, 0.9, 0.35, track=2, mouth=0.0 if i < 50 else 0.03)
        return [left, right]
    plans, shots = plan_frames(_fake_analysis(120, faces), punch=False)
    assert shots[0][2] == "crop"
    assert plans[20].cx < 0.4 and plans[-1].cx > 0.6
    jumps = np.abs(np.diff([p.cx for p in plans]))
    assert (jumps > 0.2).sum() == 1


def test_plan_shows_the_centre_square_of_faceless_shots_and_punches_on_joins():
    def faces(i):
        return [FaceObs(0.45, 0.25, 0.12, 0.25, 0.9, 0.33, track=1)] if i >= 50 else []
    plans, shots = plan_frames(_fake_analysis(150, faces, cuts=(0, 50)), join_frames=[100], punch=True)
    assert plans[10].mode == "square" and plans[60].mode == "crop"
    assert plans[99].zoom == 1.0 and plans[100].zoom > 1.05


def test_compose_shapes():
    frame = np.random.randint(0, 255, (1080, 1920, 3), np.uint8)
    assert CropCompositor().compose(frame, FramePlan("crop", 0.3, 0.5, 1.1)).shape == (1920, 1080, 3)
    assert FitCompositor().compose(frame, FramePlan("fit")).shape == (1920, 1080, 3)
    square = FitCompositor(square=True).compose(frame, FramePlan("square"))
    assert square.shape == (1920, 1080, 3) and (square[420:1500] == cover(frame, 1080, 1080)).all()


def test_cuda_folders_join_path_only_once(tmp_path, monkeypatch):
    import os
    import sys
    import types

    from clippy import transcribe

    (tmp_path / "cublas" / "bin").mkdir(parents=True)
    nvidia = types.ModuleType("nvidia")
    nvidia.__path__ = [str(tmp_path)]
    monkeypatch.setitem(sys.modules, "nvidia", nvidia)
    monkeypatch.setattr(transcribe.sys, "platform", "win32")
    monkeypatch.setattr(transcribe.os, "add_dll_directory", lambda path: None, raising=False)
    monkeypatch.setenv("PATH", "start")
    try:
        for _ in range(3):
            transcribe._add_cuda_dll_dirs.cache_clear()
            transcribe._add_cuda_dll_dirs()
    finally:
        transcribe._add_cuda_dll_dirs.cache_clear()
    assert os.environ["PATH"].split(os.pathsep) == [str(tmp_path / "cublas" / "bin"), "start"]


def test_a_cpu_fallback_after_a_cuda_failure_keeps_the_transcript_cached(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from clippy import transcribe

    segment = SimpleNamespace(start=0.0, end=1.0, text="Szia",
                              words=[SimpleNamespace(start=0.0, end=0.5, word=" Szia", probability=0.9)])

    class Model:
        def __init__(self, device):
            self.device = device

        def detect_language(self, audio, **kwargs):
            return "hu", 0.9, [("hu", 0.9)]

        def transcribe(self, audio, **kwargs):
            if self.device == "cuda":
                raise RuntimeError("Library cudnn_ops64_9.dll is not found")
            return iter([segment]), SimpleNamespace(language="hu", duration=1.0)

    monkeypatch.setattr(transcribe, "cuda_available", lambda: True)
    monkeypatch.setattr(transcribe, "_load_model", lambda name, device: (Model(device), device))
    monkeypatch.setattr(transcribe, "decode_audio", lambda path: np.zeros(16000, np.float32))
    video = tmp_path / "video.mkv"
    video.write_bytes(b"x")
    first = transcribe.transcribe(video, tmp_path)
    assert [w["word"] for w in first["segments"][0]["words"]] == ["Szia"]
    monkeypatch.setattr(transcribe, "_load_model", lambda name, device: pytest.fail("transcribed again"))
    assert transcribe.transcribe(video, tmp_path) == first


def test_romanize_for_aligner():
    from clippy.align import romanize

    assert romanize("Előadója,") == "eloadoja"
    assert romanize("10-ben") == "ben"
    assert romanize("2026") == "*"


def test_merge_hungarian_number_suffix():
    ws = [{"start": 0, "end": 0.3, "word": "10"}, {"start": 0.3, "end": 0.6, "word": "-ben"}]
    assert [w["word"] for w in merge_suffixes(ws)] == ["10-ben"]


@pytest.mark.skipif(not SAMPLE.exists(), reason="sample video missing")
def test_two_pass_loudness_measurement():
    m = aud.measure(SAMPLE, 2.0, 8.0, [(0.0, 3.0), (4.0, 8.0)], "voice")
    assert m is not None and -40 < float(m["input_i"]) < 0


def test_text_helpers():
    assert ends_sentence("done.”") and ends_sentence("why?") and not ends_sentence("well,")
    assert ends_clause("well,") and ends_clause("note:") and not ends_clause("and")
    assert clean_text("10 -ben  ,  ok") == "10-ben, ok"
    assert Clip.from_dict({"start": 1, "end": 5, "score": 9, "duration": 4, "extra": 1}).end == 5


def test_prepare_fonts_only_reports_a_font_that_is_really_missing(tmp_path, monkeypatch):
    from clippy import render
    src = tmp_path / "caption.ttf"
    src.write_bytes(b"font")
    folder = tmp_path / "fonts"
    logged = []
    monkeypatch.setattr(render, "ROOT", tmp_path)
    monkeypatch.setattr(render, "system_fonts", lambda: [src])
    monkeypatch.setattr(render, "log", logged.append)

    def raced(_src, _dst):
        (folder / "caption.ttf").write_bytes(b"font")
        raise PermissionError("in use by another render")

    monkeypatch.setattr(render.shutil, "copyfile", raced)
    assert render.prepare_fonts(folder) == folder
    assert logged == []

    (folder / "caption.ttf").unlink()

    def failed(_src, _dst):
        raise PermissionError("denied")

    monkeypatch.setattr(render.shutil, "copyfile", failed)
    render.prepare_fonts(folder)
    assert len(logged) == 1 and "caption.ttf skipped" in logged[0]


def _scenes(kind: str, x: int = 320) -> list:
    from PIL import Image, ImageDraw, ImageFont

    from clippy.render import system_fonts

    rng = np.random.default_rng(1)
    font = ImageFont.truetype(str(system_fonts()[0]), 34)
    words = ["THAT WAS INSANE", "NO WAY BRO", "WATCH THIS", "HE DID IT", "OH MY GOD", "LETS GO", "WHAT IS THAT",
             "RUN RUN", "NOT AGAIN", "CLUTCH"]
    out = []
    for i, word in enumerate(words):
        im = Image.fromarray((rng.random((360, 640, 3)) * 120 + 40).astype(np.uint8))
        text = {"captions": word, "hud": "HP 100  AMMO 30"}.get(kind, "")
        if text:
            ImageDraw.Draw(im).text((x, 300), text, font=font, fill="white", stroke_width=3, stroke_fill="black",
                                    anchor="mm")
        out.append(np.asarray(im))
    return out


def test_captions_burned_into_the_source_are_found_but_a_hud_or_a_side_chat_is_not():
    from clippy.burned import caption_zone

    assert caption_zone(_scenes("captions")) is not None
    assert caption_zone(_scenes("hud")) is None and caption_zone(_scenes("none")) is None
    assert caption_zone(_scenes("captions", x=110)) is None


def test_a_stream_shot_follows_the_scene_when_the_camera_is_not_where_it_was_found():
    from clippy.director import plan_frames

    cam = (0.75, 0.05, 0.2, 0.2)

    def faces(i):
        if i < 100:
            return [FaceObs(0.82, 0.1, 0.05, 0.09, 0.9, 0.12)]
        return [FaceObs(0.35, 0.2, 0.3, 0.5, 0.9, 0.35)]

    an = _fake_analysis(200, faces, cuts=[0, 100])
    plans, shots = plan_frames(an, "stream", punch=False, facecam=cam)
    assert [m for _, _, m in shots] == ["stream", "crop"]
    assert plans[50].mode == "stream" and plans[150].mode == "crop"


def test_the_loudest_reaction_shows_the_camera_full_screen():
    from clippy.director import plan_frames, reaction_windows

    cam = (0.75, 0.05, 0.2, 0.2)
    an = _fake_analysis(750, lambda i: [FaceObs(0.82, 0.1, 0.05, 0.09, 0.9, 0.12, mouth=0.6 if 300 <= i < 330 else 0.1)])
    env = np.full(3000, -40.0)
    env[1200:1300] = -10.0
    windows = reaction_windows(an, env, 0.01, cam)
    assert len(windows) == 1 and 300 <= windows[0][0] < 330 < windows[0][1]
    assert reaction_windows(an, env, 0.01, None) == [] and reaction_windows(an, np.full(3000, -40.0), 0.01, cam) == []
    plans, _ = plan_frames(an, "stream", punch=False, facecam=cam, reactions=windows)
    assert plans[windows[0][0] + 1].mode == "cam" and plans[100].mode == "stream"


def test_the_camera_compositor_and_the_title_bar():
    from clippy.compositing import OUT_H, OUT_W, CamCompositor
    from clippy.director import FramePlan
    from clippy.render import title_bar

    frame = np.zeros((1080, 1920, 3), np.uint8)
    frame[54:270, 1440:1824] = 200
    out = CamCompositor((0.75, 0.05, 0.2, 0.2)).compose(frame, FramePlan("cam"))
    assert out.shape == (OUT_H, OUT_W, 3) and out[OUT_H // 2, OUT_W // 2].mean() > 150
    assert title_bar("", 10, True) == ()
    [bar] = title_bar("He did it", 12.0, True)
    assert bar.end == 12.0 and bar.y < 400 and bar.box


def test_a_stream_without_a_cut_shows_the_whole_frame_while_the_camera_spot_is_empty():
    from clippy.director import plan_frames

    cam = (0.75, 0.05, 0.2, 0.2)
    an = _fake_analysis(250, lambda i: [FaceObs(0.82, 0.1, 0.05, 0.09, 0.9, 0.12)] if i < 150 else [])
    plans, shots = plan_frames(an, "stream", punch=False, facecam=cam)
    assert len(shots) == 1 and plans[50].mode == "stream" and plans[230].mode == "fit"


def test_a_face_in_the_middle_is_a_full_screen_camera_not_a_facecam_overlay():
    from clippy.director import guess_facecam, mostly_faces

    middle = _fake_analysis(100, lambda i: [FaceObs(0.45, 0.4, 0.1, 0.18, 0.9, 0.45)])
    corner = _fake_analysis(100, lambda i: [FaceObs(0.05, 0.75, 0.06, 0.1, 0.9, 0.78)])
    assert guess_facecam(middle) is None and mostly_faces(middle)
    assert guess_facecam(corner) is not None
    assert not mostly_faces(_fake_analysis(100, lambda i: []))


def test_tightening_keeps_a_natural_pause_instead_of_cutting_it_away():
    ws = [{"word": "First", "start": 0.0, "end": 0.4}, {"word": "line.", "start": 0.5, "end": 0.9},
          {"word": "Then", "start": 3.0, "end": 3.3}, {"word": "this", "start": 3.4, "end": 3.7},
          {"word": "goes", "start": 5.0, "end": 5.3}, {"word": "on.", "start": 5.4, "end": 5.8}]
    ep = build_edit(ws, 0.0, 6.0, 30.0)
    kept = [round(b2 - a2, 2) for (_, a2), (b2, _) in zip(ep.intervals, ep.intervals[1:])]
    gaps = [ep.words[k + 1]["start"] - ep.words[k]["end"] for k in range(len(ep.words) - 1)]
    after_sentence, inside = gaps[1], gaps[3]
    assert 0.45 <= after_sentence <= 0.56 and 0.27 <= inside <= 0.37
    assert len(ep.intervals) == 3 and kept


def test_a_title_full_of_emoji_gives_whisper_only_its_words_and_emoji_never_become_words():
    from clippy.text import hint_words, spoken
    assert hint_words("🚨🚨 Jonathan Blow made me quit my job 🚨🚨 @theprimeagen #coding https://x.com/a") == \
        "Jonathan Blow made me quit my job"
    words = [{"word": "🚨🚨", "start": 0, "end": 1}, {"word": "🚨Find", "start": 1, "end": 2},
             {"word": "job.", "start": 2, "end": 3}, {"word": "❤️", "start": 3, "end": 4}]
    assert [w["word"] for w in spoken(words)] == ["Find", "job."]


def test_a_stream_shot_shorter_than_the_camera_check_never_reaches_past_the_clip():
    from clippy.director import cam_gone, plan_frames
    an = _fake_analysis(n=30)
    assert cam_gone(an, 0, 30, (0.0, 0.0, 0.3, 0.3)) == []
    plans, _ = plan_frames(an, "stream", facecam=(0.0, 0.0, 0.3, 0.3))
    assert len(plans) == 30


def test_a_failed_ffmpeg_run_and_an_internal_error_read_as_plain_words():
    from clippy import i18n
    from clippy.tools import ffmpeg_failed
    from clippy.web.handlers import short_error
    memory = ffmpeg_failed("finishing the edit", "[fc#0] Error requesting a frame: Cannot allocate memory\n")
    other = ffmpeg_failed("rendering the clip", "Invalid argument\n")
    assert (memory.key, other.key) == ("error.ffmpeg_memory", "error.ffmpeg_failed")
    assert "memory" in i18n.t(memory.key, **memory.params) and "[fc#0]" not in short_error(memory)
    text = short_error(IndexError("list assignment index out of range"))
    assert "internal error (IndexError)" in text and "list assignment" not in text


def test_the_facecam_is_the_corner_face_not_the_people_in_a_video_the_streamer_watches():
    from clippy.director import guess_facecam
    watched = [FaceObs(0.19, 0.27, 0.045, 0.11, 0.9, 0.3), FaceObs(0.48, 0.23, 0.043, 0.1, 0.9, 0.27),
               FaceObs(0.81, 0.24, 0.045, 0.11, 0.9, 0.28)]
    camera = FaceObs(0.78, 0.72, 0.07, 0.15, 0.9, 0.76)
    an = _fake_analysis(100, lambda i: watched + ([camera] if i % 10 else []))
    x, y, w, h = guess_facecam(an)
    assert x < 0.81 < x + w and y < 0.79 < y + h
