import io
import os
import subprocess
import sys
import wave
from pathlib import Path

import cv2
import numpy as np
import pytest

from clippy.editing.assets import AssetService
from clippy.editing.editor import Editor
from clippy.editing.factory import timeline_from_clip
from clippy.editing.operations import EditContext, OperationCatalogue
from clippy.editing.render.base import BaseRender, BaseStore
from clippy.editing.render.finishing import FINAL, FinishingRenderer, RenderProfile
from clippy.editing.render.planner import TimelineRenderer
from clippy.editing.timeref import TimeResolver
from clippy.render import Encoder, fonts_dir_for, prepare_fonts
from clippy.storage import Storage
from clippy.tools import ffmpeg_path, probe_media

PREVIEW = RenderProfile("preview", 0.5, False, "128k")
WORDS = [{"start": 0.2, "end": 0.6, "word": "Ez"}, {"start": 0.7, "end": 1.2, "word": "egy"},
         {"start": 1.3, "end": 2.0, "word": "próba."}]


def ffmpeg(*args):
    subprocess.run([ffmpeg_path(), "-v", "error", "-y", *args], check=True)


def solid_png(bgr, size=(64, 64)) -> bytes:
    return cv2.imencode(".png", np.full((size[1], size[0], 3), bgr, np.uint8))[1].tobytes()


def tone(seconds=8.0) -> bytes:
    out = io.BytesIO()
    t = np.arange(int(48000 * seconds)) / 48000
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes((np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16).tobytes())
    return out.getvalue()


def frame_at(video: Path, t: float, out: Path) -> np.ndarray:
    ffmpeg("-ss", f"{t}", "-i", str(video), "-frames:v", "1", str(out))
    return cv2.imread(str(out))


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    root = tmp_path_factory.mktemp("render")
    base_video = root / "base.mkv"
    ffmpeg("-f", "lavfi", "-i", "color=c=gray:s=1080x1920:r=25:d=5", "-f", "lavfi", "-i",
           "sine=frequency=220:sample_rate=48000:duration=5", "-ac", "2", "-c:v", "libx264", "-preset", "ultrafast",
           "-pix_fmt", "yuv420p", "-c:a", "flac", str(base_video))
    base = BaseRender(base_video, 5.0, 25.0, "25", WORDS, ((10.0, 15.0),), {"duration_out": 5.0})
    service = AssetService(Storage(root / "data", root / "library"))
    logo = service.add(1, "logo.png", io.BytesIO(solid_png((0, 0, 255))))
    outro = service.add(1, "outro.png", io.BytesIO(solid_png((255, 0, 0), (108, 192))))
    music = service.add(1, "song.wav", io.BytesIO(tone()))
    timeline = timeline_from_clip({"start": 10, "end": 15, "hook": "Ezt nézd: [teszt]; it's 100%"},
                                  {"cta": "Kövess!"})
    ctx = EditContext(assets=service.index(1), resolver=TimeResolver(5.0, WORDS), clock=base.clock,
                      source_duration=60.0)
    ops, errors = OperationCatalogue().parse([
        {"kind": "color", "contrast": 1.2, "temperature": 5000},
        {"kind": "add_overlay", "asset": logo.id, "position": "top-right", "width": 0.2},
        {"kind": "add_text", "text": "Holnap: 2. rész", "position": "center"},
        {"kind": "add_music", "asset": music.id, "gain_db": -12},
        {"kind": "set_outro", "asset": outro.id, "transition": "fade", "transition_duration": 0.5},
    ])
    assert not errors
    result = Editor().run(timeline, ops, ctx)
    assert result.ok
    fonts = prepare_fonts(root / "fonts")
    return root, base, service.index(1), result.timeline, fonts


def render(setup, profile, name):
    root, base, assets, timeline, fonts = setup
    work = root / f"work-{name}"
    work.mkdir()
    out = root / f"{name}.mp4"
    info = FinishingRenderer(Encoder.choose("auto")).render(timeline, base, assets, work, fonts_dir_for(work, fonts),
                                                            out, profile)
    return out, info


def test_preview_has_every_layer_and_the_outro(setup):
    out, info = render(setup, PREVIEW, "preview")
    media = probe_media(out)
    assert media["video"] == (540, 960) and media["audio"]
    assert abs(media["duration"] - 7.5) < 0.15 and abs(info["duration_out"] - 7.5) < 0.01
    assert out.with_suffix(".jpg").exists()


def test_final_places_the_logo_and_ends_on_the_outro(setup):
    root = setup[0]
    out, info = render(setup, FINAL, "final")
    assert probe_media(out)["video"] == (1080, 1920) and info["loudness_in"] is not None
    frame = frame_at(out, 2.0, root / "f2.png")
    b, g, r = frame[int(0.09 * 1920), int(0.8 * 1080)].astype(int)
    assert r > 150 and g < 90 and b < 90
    b, g, r = frame_at(out, 7.2, root / "f7.png")[960, 540].astype(int)
    assert b > 150 and r < 90


def test_cached_base_is_reused_without_rendering_again(setup, tmp_path):
    root, base, assets, timeline, _ = setup
    store = BaseStore(tmp_path / "base")
    (tmp_path / "rendered.mkv").write_bytes(b"x")
    store.put(timeline.base_key(), BaseRender(tmp_path / "rendered.mkv", 1, 25, "25", [], ((0.0, 1.0),), {}))
    assert not (tmp_path / "rendered.mkv").exists() and store.paths(timeline.base_key())[0].exists()

    class Project:
        storage = Storage(tmp_path / "data", tmp_path / "library")
        scratch_root = None

    renderer = TimelineRenderer(Project(), store)
    cached, reused = renderer.base(timeline)
    assert reused and cached.duration == 1


def edited(setup, ops, number=1, clips=None):
    root, base, _, _, _ = setup
    service = AssetService(Storage(root / "data", root / "library"))
    timeline = timeline_from_clip({"start": 10, "end": 15, "hook": "Figyeld!"}, {})
    ctx = EditContext(assets=service.index(1), resolver=TimeResolver(5.0, WORDS), clock=base.clock,
                      source_duration=60.0, number=number, clips=clips or {})
    parsed, errors = OperationCatalogue().parse(ops)
    assert not errors
    result = Editor().run(timeline, parsed, ctx)
    assert result.ok, result.issues
    return result.timeline, service.index(1)


def render_timeline(setup, timeline, assets, name, profile=PREVIEW, **extra):
    root, base, _, _, fonts = setup
    work = root / f"work-{name}"
    work.mkdir()
    out = root / f"{name}.mp4"
    info = FinishingRenderer(Encoder.choose("auto")).render(timeline, base, assets, work, fonts_dir_for(work, fonts),
                                                            out, profile, **extra)
    return out, info


@pytest.mark.skipif(sys.platform == "darwin" and bool(os.environ.get("CI")),
                    reason="GitHub's macOS runners kill this ffmpeg run for memory now and then")
def test_speed_effects_stickers_sounds_broll_and_a_joined_clip_render(setup):
    root = setup[0]
    broll_file, joined = root / "broll.mp4", root / "clip2.mp4"
    for path, colour, seconds in ((broll_file, "red", 3), (joined, "blue", 2)):
        ffmpeg("-f", "lavfi", "-i", f"color=c={colour}:s=320x240:r=25:d={seconds}", "-f", "lavfi", "-i",
               f"sine=frequency=330:duration={seconds}", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt",
               "yuv420p", "-c:a", "aac", str(path))
    service = AssetService(Storage(root / "data", root / "library"))
    broll = service.add(1, "broll.mp4", io.BytesIO(broll_file.read_bytes()))
    at = lambda s: {"type": "at", "at": s}
    ops = [{"kind": "add_effect", "effect": effect, "span": {"start": at(s)}}
           for effect, s in (("zoom", 0.2), ("shake", 1.2), ("flash", 2.2), ("fade_black", 3.0))]
    ops += [{"kind": "add_effect", "effect": e, "span": {"start": at(3.0), "end": at(4.0)}}
            for e in ("black_white", "blur", "vignette")]
    ops += [{"kind": "add_sound", "sound": s, "at": at(0.5 * i)} for i, s in
            enumerate(("pop", "ding", "boom", "click", "whoosh", "riser", "bleep"))]
    ops += [{"kind": "add_sticker", "emoji": "🔥", "motion": "bounce"},
            {"kind": "add_text", "text": "Gépelt szöveg itt", "animation": "typewriter", "position": "center"},
            {"kind": "add_broll", "asset": broll.id, "span": {"start": at(1.0), "length": 1.0}, "gain_db": -12},
            {"kind": "set_speed", "factor": 2, "span": {"start": at(0), "end": at(2)}},
            {"kind": "set_speed", "factor": 0.5, "span": {"start": at(4), "end": at(5)}},
            {"kind": "join_clip", "clip": 2, "transition": "slideleft"}]
    timeline, assets = edited(setup, ops, clips={2: 1})
    out, info = render_timeline(setup, timeline, assets, "everything", joined={2: joined})
    media = probe_media(out)
    assert media["video"] == (540, 960) and media["audio"]
    assert abs(info["duration_out"] - 6.5) < 0.01 and abs(media["duration"] - 6.5) < 0.2
    b, g, r = frame_at(out, 6.3, root / "joined.png")[480, 270].astype(int)
    assert b > 150 and r < 90


@pytest.mark.parametrize("fmt, size", [("1:1", (540, 540)), ("4:5", (540, 676)), ("16:9", (960, 540))])
def test_other_aspect_ratios(setup, fmt, size):
    root = setup[0]
    source = root / "source.mp4"
    if not source.exists():
        ffmpeg("-f", "lavfi", "-i", "color=c=green:s=1920x1080:r=25:d=20", "-c:v", "libx264", "-preset", "ultrafast",
               "-pix_fmt", "yuv420p", str(source))
    timeline, assets = edited(setup, [{"kind": "set_format", "format": fmt}])
    out, _ = render_timeline(setup, timeline, assets, f"format-{fmt.replace(':', 'x')}", source=source)
    assert probe_media(out)["video"] == size
    b, g, r = frame_at(out, 2.5, root / f"{fmt.replace(':', 'x')}.png")[size[1] // 2, size[0] // 2].astype(int)
    assert (g > 100 and r < 90) if fmt == "16:9" else abs(int(g) - int(r)) < 30
