import sqlite3
import threading
import time

import pytest

from clippy import download, separation
from clippy.concurrency import GPU, RENDER, SOURCE, GuardedLLM, KeyedLock, LockOrderError, Slots, held
from clippy.highlights import Clip
from clippy.storage import Storage
from clippy.tools import SharedProgress, atomic_path, log_sink, report, write_json
from clippy.web import jobs
from clippy.web.clips import ClipLibrary, ClipPresenter
from clippy.web.db import DB
from clippy.web.hub import Hub
from clippy.web.jobs import JobHandler, Runner
from clippy.web.media import MediaStore


def wait_until(check, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if check():
            return True
        time.sleep(0.01)
    return False


def test_locks_are_taken_only_in_the_global_order():
    with SOURCE.hold("vid"), RENDER.hold(), GPU.hold():
        assert held() == ("source:vid", "render", "gpu")
    with GPU.hold():
        with pytest.raises(LockOrderError):
            with RENDER.hold():
                pass
    with SOURCE.hold("a"):
        with pytest.raises(LockOrderError):
            with SOURCE.hold("b"):
                pass
    assert held() == ()


def test_same_key_waits_other_keys_run_in_parallel():
    lock, inside, peak, guard = KeyedLock("k", 1), {}, {}, threading.Lock()

    def work(key):
        with lock.hold(key):
            with guard:
                inside[key] = inside.get(key, 0) + 1
                peak[key] = max(peak.get(key, 0), inside[key])
                peak["all"] = max(peak.get("all", 0), sum(inside.values()))
            time.sleep(0.05)
            with guard:
                inside[key] -= 1

    threads = [threading.Thread(target=work, args=(k,)) for k in ("a", "a", "a", "b", "b")]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert peak["a"] == 1 and peak["b"] == 1 and peak["all"] == 2
    assert not lock.busy("a") and not lock.busy("b")


def test_slots_cap_how_many_run_at_once():
    slots, running, peak, guard = Slots("s", 2, 1), [0], [0], threading.Lock()

    def work():
        with slots.hold():
            with guard:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.03)
            with guard:
                running[0] -= 1

    threads = [threading.Thread(target=work) for _ in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert peak[0] == 2


def test_llm_calls_run_alone_on_the_gpu():
    class LLM:
        backend = "fake"

        def chat_json(self, *a, **k):
            return held()

    guarded = GuardedLLM(LLM())
    assert guarded.chat_json("s", "u") == ("gpu",) and guarded.backend == "fake"


def test_atomic_writes_leave_the_old_file_or_the_new_one(tmp_path):
    target = tmp_path / "x.transcript.json"
    write_json(target, {"v": 1})
    with pytest.raises(TypeError):
        write_json(target, {"v": object()})
    assert target.read_text(encoding="utf-8").strip().endswith("}") and '"v": 1' in target.read_text(encoding="utf-8")
    with pytest.raises(RuntimeError):
        with atomic_path(tmp_path / "clip.mp4") as tmp:
            tmp.write_bytes(b"half")
            assert tmp.name.startswith(".") and tmp.suffix == ".mp4"
            raise RuntimeError("encoder died")
    assert [p.name for p in tmp_path.iterdir()] == ["x.transcript.json"]


def test_a_failed_separation_leaves_no_stems_behind(tmp_path, monkeypatch):
    def broken(video, vocals, music, chunk, pad):
        vocals.write_bytes(b"part")
        music.write_bytes(b"part")
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(separation, "_separate", broken)
    with pytest.raises(RuntimeError):
        separation.separate(tmp_path / "vid.mkv", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_only_a_finished_download_counts(tmp_path):
    (tmp_path / "vid.f137.mp4").write_bytes(b"fragment")
    (tmp_path / ".part-vid-1234").mkdir()
    assert download._downloaded(tmp_path, "vid") is None
    (tmp_path / "vid.mkv").write_bytes(b"merged")
    assert download._downloaded(tmp_path, "vid") == tmp_path / "vid.mkv"


def test_a_version_number_can_be_stored_only_once(tmp_path):
    db = DB(tmp_path / "t.db")
    chat = db.create_chat()
    db.add_clip(chat["id"], 1, 1, {}, {}, {}, "a.mp4", "a.jpg", "a.txt")
    with pytest.raises(sqlite3.IntegrityError):
        db.add_clip(chat["id"], 1, 1, {}, {}, {}, "b.mp4", "b.jpg", "b.txt")


def test_parallel_parts_report_their_mean():
    seen = []
    with log_sink(lambda line: None, seen.append):
        shared = SharedProgress(2)
        shared.wrap(0, lambda: report(1.0))()
        shared.wrap(1, lambda: report(0.5))()
    assert seen[-1] == pytest.approx(0.75)


class Gate(JobHandler):
    kind = "gate"
    stages = ("edit",)
    events: dict = {}
    log: list = []

    def run(self, ctx):
        name = ctx.job.payload["name"]
        Gate.log.append(("start", name))
        assert Gate.events[name].wait(5)
        Gate.log.append(("end", name))
        return {"key": "done.nothing", "params": {}}


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setitem(jobs.HANDLERS, "gate", Gate())
    monkeypatch.setattr(jobs, "job_text", lambda kind, payload: {"key": "job.edit", "params": {"clip": 1}})
    Gate.events, Gate.log = {}, []
    storage = Storage(tmp_path / "data", tmp_path / "library")
    db = DB(tmp_path / "t.db")
    return Runner(db, Hub(), storage, ClipPresenter(MediaStore(storage)), ClipLibrary(storage), workers=3)


def test_chats_run_in_parallel_but_one_chat_keeps_its_order(runner):
    a, b = runner.db.create_chat()["id"], runner.db.create_chat()["id"]
    for name in ("a1", "a2", "b1"):
        Gate.events[name] = threading.Event()
    runner.submit(a, "gate", {"name": "a1"})
    runner.submit(a, "gate", {"name": "a2"})
    runner.submit(b, "gate", {"name": "b1"})
    assert wait_until(lambda: {("start", "a1"), ("start", "b1")} <= set(Gate.log))
    assert ("start", "a2") not in Gate.log and runner.status()["queue"][0]["chat_id"] == a
    assert {j["chat_id"] for j in runner.status()["running"]} == {a, b}
    Gate.events["b1"].set()
    Gate.events["a1"].set()
    assert wait_until(lambda: ("start", "a2") in Gate.log)
    assert Gate.log.index(("end", "a1")) < Gate.log.index(("start", "a2"))
    Gate.events["a2"].set()
    assert wait_until(lambda: not runner.status()["busy"])


class SlowProject:
    def __init__(self, source, opts, storage, scratch_root=None):
        self.source, self.opts, self.storage, self.video_stem = source, opts, storage, "vid"
        self.title, self.transcript = "Video", {"language": "en"}

    def prepare(self):
        return self

    def select(self, n=None, exclude=(), instructions=None, opts=None):
        return [Clip(10.0 * i, 10.0 * i + 5, 90 - i, hook=f"H{i}", title=f"T{i}") for i in range(n or 3)]

    def prefetch(self, clip):
        pass

    def render(self, clip, dest, name, overrides=None, keep_base=False):
        time.sleep(0.15 if clip.start == 0 else 0.01)
        mp4 = dest / f"{name}.mp4"
        for suffix, data in ((".mp4", b"v"), (".jpg", b"j"), (".txt", b"t")):
            mp4.with_suffix(suffix).write_bytes(data)
        return {"mp4": str(mp4), "jpg": str(mp4.with_suffix(".jpg")), "txt": str(mp4.with_suffix(".txt")),
                "duration_out": 5.0, "dead_air_removed": 0.0}


def test_parallel_renders_are_stored_in_clip_order(tmp_path):
    storage = Storage(tmp_path / "data", tmp_path / "library")
    db = DB(tmp_path / "t.db")
    runner = Runner(db, Hub(), storage, ClipPresenter(MediaStore(storage)), ClipLibrary(storage),
                    project_factory=SlowProject, start=False)
    chat = db.create_chat()["id"]
    db.update_chat(chat, source="https://youtu.be/x")
    runner.run_one(runner.submit(chat, "more", {"n": 3}))
    rows = sorted(db.current_clips(chat), key=lambda r: r["id"])
    assert [(r["number"], r["clip"]["hook"]) for r in rows] == [(1, "H0"), (2, "H1"), (3, "H2")]


def test_a_new_video_is_transcribed_while_it_still_downloads(tmp_path, monkeypatch):
    from clippy import pipeline
    from clippy.download import Remote

    started, release, decoded = threading.Event(), threading.Event(), []

    def slow_video(remote, downloads, cookies_from_browser=None):
        started.set()
        assert release.wait(5)
        (downloads / "vid.mkv").write_bytes(b"video")
        return downloads / "vid.mkv"

    def fake_transcribe(video, cache, **kw):
        decoded.append((video.name, kw["sound"].name, release.is_set()))
        return {"language": "en", "segments": [{"words": [{"word": "hi", "start": 0, "end": 1}]}]}

    monkeypatch.setattr(pipeline, "probe", lambda *a, **k: Remote("https://x", {"requested_formats": [{"format_id": "251", "vcodec": "none", "acodec": "opus"}]}, "vid", "Title"))
    monkeypatch.setattr(pipeline, "cached_video", lambda *a: None)
    monkeypatch.setattr(pipeline, "fetch_video", slow_video)
    monkeypatch.setattr(pipeline, "fetch_audio", lambda remote, downloads, cookies=None: downloads / "vid.audio.webm")
    monkeypatch.setattr(pipeline, "transcribe", fake_transcribe)
    monkeypatch.setattr(pipeline, "probe_media", lambda path: {"audio": True})
    opts = pipeline.Options(isolate="off", align=False)
    proj = pipeline.Project("https://x", opts, Storage(tmp_path / "data", tmp_path / "lib"))
    proj.prepare()
    assert started.wait(5) and decoded == [("vid.mkv", "vid.audio.webm", False)]
    assert proj.video_stem == "vid" and proj.words[0]["word"] == "hi"
    release.set()
    assert proj.video.name == "vid.mkv"


def test_a_strip_decode_gives_exactly_the_same_crop():
    import numpy as np

    from clippy.compositing import CropCompositor
    from clippy.director import FramePlan

    rng = np.random.default_rng(7)
    frame = rng.integers(0, 255, (1200, 2400, 3), dtype=np.uint8)
    plans = [FramePlan("crop", cx, 0.5, zoom) for cx, zoom in ((0.40, 1.0), (0.45, 1.1), (0.52, 1.0))]
    x0, x1 = CropCompositor.columns(2400, 1200, plans)
    assert x0 % 2 == 0 and x1 % 2 == 0 and x1 - x0 < 0.8 * 2400
    rebuilt = rng.integers(0, 255, frame.shape, dtype=np.uint8)
    rebuilt[:, x0:x1] = frame[:, x0:x1]
    crop = CropCompositor()
    for plan in plans:
        assert np.array_equal(crop.compose(frame, plan), crop.compose(rebuilt, plan))
    assert CropCompositor.columns(2400, 1200, plans + [FramePlan("fit")]) is None
    assert CropCompositor.columns(2400, 1200, [FramePlan("crop", 0.1), FramePlan("crop", 0.9)]) is None


class BaseProject(SlowProject):
    layout = "auto"

    def render(self, clip, dest, name, overrides=None, keep_base=False):
        info = super().render(clip, dest, name, overrides)
        hidden = dest / f".{name}.base.mkv"
        hidden.write_bytes(b"base")
        info["base"] = {"video": str(hidden), "words": [{"word": "hi", "start": 0, "end": 1}],
                        "intervals": [(clip.start, clip.end)], "fps": 25.0, "rate": "25/1", "layout": self.layout,
                        "punch": True, "tighten": True, "audio": "voice", "duck": 0.0}
        return info


@pytest.mark.parametrize("layout, kept", [("auto", 3), ("blur", 0)])
def test_the_base_made_with_a_clip_is_kept_only_when_the_editor_wants_it(tmp_path, layout, kept):
    from clippy.editing.render.base import BaseStore
    from clippy.editing.repository import TimelineRepository

    BaseProject.layout = layout
    storage = Storage(tmp_path / "data", tmp_path / "library")
    db = DB(tmp_path / "t.db")
    runner = Runner(db, Hub(), storage, ClipPresenter(MediaStore(storage)), ClipLibrary(storage),
                    project_factory=BaseProject, start=False)
    chat = db.create_chat()["id"]
    db.update_chat(chat, source="https://youtu.be/x")
    runner.run_one(runner.submit(chat, "more", {"n": 3}))
    store = BaseStore(storage.chat_base(chat))
    found = [store.get(TimelineRepository(db).of(r, {}).base_key()) for r in db.current_clips(chat)]
    assert sum(b is not None for b in found) == kept
    assert not list(storage.chat_clips(chat).glob(".*"))
