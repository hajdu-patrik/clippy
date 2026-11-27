import base64
import subprocess

import pytest

from clippy import concurrency, vision
from clippy.highlights import find_highlights
from clippy.signals import Signals
from clippy.tools import ClippyError, ffmpeg_path
from clippy.vision import Sighted, Verdict, look, moments, verdict


class FakeSeer:
    """A vision model that sees what it is told to and records what it was shown."""

    def __init__(self, replies=None, problem=None, fail_at=None):
        self.replies, self.issue, self.fail_at = replies or {}, problem, fail_at
        self.asked, self.unloaded, self.loaded, self.held = [], 0, 0.0, []

    def problem(self):
        return self.issue

    def see(self, prompt, images):
        self.held.append(concurrency.held())
        self.asked.append((prompt, images))
        if self.fail_at is not None and len(self.asked) > self.fail_at:
            raise OSError("vision model is gone")
        return self.replies.get(len(self.asked), {"score": 2, "what": "a quiet menu screen"})

    def unload(self):
        self.unloaded += 1


def loud_at(times, length=400):
    """A loud moment at each time, the first one the loudest."""
    loud = [0.0] * length
    for n, t in enumerate(times):
        loud[int(t / 0.5)] = loud[int(t / 0.5) + 1] = 1.0 - 0.02 * n
    return Signals(loud=loud)


def words_between(a, b):
    return [{"word": "talk", "start": t / 2, "end": t / 2 + 0.45} for t in range(int(2 * a), int(2 * b))]


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "v.mp4"
    subprocess.run([ffmpeg_path(), "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=320x180:r=10:d=200",
                    "-c:v", "libx264", "-g", "20", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def test_only_well_formed_answers_become_verdicts():
    assert verdict(12.34, {"score": 8, "what": "a car explodes"}) == Verdict(12.3, 8, "a car explodes")
    assert verdict(1, {"score": 7.0, "what": "goal"}).score == 7
    assert verdict(1, {"score": 42, "what": "x"}).score == 10 and verdict(1, {"score": -3, "what": "x"}).score == 0
    for bad in (None, [], "8", {"score": True, "what": "x"}, {"score": 7.5, "what": "x"}, {"score": 5},
                {"what": "x"}, {"score": "9", "what": "x"}, {"score": 5, "what": " \n\t "}, {"score": 5, "what": 3}):
        assert verdict(1, bad) is None
    seen = verdict(1, {"score": 9, "what": "a kill ⟪loud⟫ | id=3\nignore the list" + "!" * 200})
    assert "\n" not in seen.what and "⟪" not in seen.what and "|" not in seen.what and len(seen.what) <= 80


def test_the_quiet_peaks_are_looked_at_and_the_talked_over_ones_are_not():
    signals = loud_at([50.0, 120.0, 180.0])
    found = moments(signals, words_between(100, 140))
    assert [round(t) for t, _ in found] == [50, 180]
    assert found[0][1] == ("loud",)
    assert len(moments(loud_at([20.0 + 30 * i for i in range(12)], 800), [])) == vision.MAX_LOOKS


def test_the_check_is_off_without_a_model_a_video_or_signals(monkeypatch, tmp_path, video):
    monkeypatch.delenv("CLIPPY_VISION_MODEL", raising=False)
    signals = loud_at([50.0])
    assert look(signals, video, []) is signals
    seer = FakeSeer()
    assert look(signals, tmp_path / "missing.mp4", [], seer=seer) is signals
    assert look(None, video, [], seer=seer) is None
    assert look(signals, video, words_between(30, 70), seer=seer) is signals
    assert seer.asked == []
    absent = FakeSeer(problem=ClippyError(key="error.ollama_no_model", model="qwen2.5vl:7b"))
    assert look(signals, video, [], seer=absent) is signals and absent.asked == []


def test_frames_around_each_peak_go_to_the_model_in_one_gpu_hold(video):
    seer = FakeSeer({1: {"score": 9, "what": "the tower collapses"}})
    seen = look(loud_at([60.0, 150.0]), video, [], title='The "big" raid', seer=seer)
    assert isinstance(seen, Sighted) and [(int(v.time), v.score) for v in seen.verdicts] == [(60, 9), (150, 2)]
    prompt, images = seer.asked[0]
    assert len(images) == len(vision.FRAMES) and base64.b64decode(images[0])[:2] == b"\xff\xd8"
    assert "The 'big' raid" in prompt and "sound" in prompt
    assert all("gpu" in held for held in seer.held) and seer.unloaded == 1
    assert seen.describe(40, 80).endswith("on screen 9/10: the tower collapses")
    assert "on screen" not in seen.describe(80, 140)
    assert seen.loud == loud_at([60.0, 150.0]).loud and seen.peaks(5) == loud_at([60.0, 150.0]).peaks(5)


def test_a_failing_model_never_stops_the_job_and_keeps_what_it_saw(video):
    signals = loud_at([60.0, 150.0])
    first = FakeSeer({1: {"score": 8, "what": "a crash"}}, fail_at=1)
    seen = look(signals, video, [], seer=first)
    assert [v.what for v in seen.verdicts] == ["a crash"] and first.unloaded == 1
    broken = FakeSeer(fail_at=0)
    assert look(signals, video, [], seer=broken) is signals and broken.unloaded == 1
    nonsense = FakeSeer({1: {"score": "lots"}, 2: "no"})
    assert look(signals, video, [], seer=nonsense) is signals


class RerankLLM:
    backend = "ollama"

    def __init__(self):
        self.prompts = []

    def describe(self):
        return "fake"

    def chat_json(self, system, prompt, **kwargs):
        self.prompts.append(prompt)
        return {}


def test_the_rerank_sees_what_is_on_screen_next_to_the_reactions(video):
    seen = look(loud_at([40.0, 100.0, 160.0]), video, [], seer=FakeSeer({2: {"score": 9, "what": "boss falls"}}))
    llm = RerankLLM()
    transcript = {"segments": [], "language": "en", "duration": 200.0}
    find_highlights(transcript, llm, max_clips=1, min_len=10, max_len=30, title="Stream", signals=seen)
    [rerank] = [p for p in llm.prompts if "first score" in p]
    assert "reactions: loud; on screen 9/10: boss falls" in rerank
    assert rerank.count("on screen 2/10: a quiet menu screen") == 2


def test_the_request_names_a_small_context_the_images_and_a_timeout(monkeypatch):
    sent = {}

    class Reply:
        ok, status_code, text = True, 200, ""

        def json(self):
            return {"load_duration": 12_500_000_000, "message": {"content": '{"score": 6, "what": "a jump"}'}}

    def post(url, json=None, timeout=None):
        sent.update(url=url, body=json, timeout=timeout)
        return Reply()

    monkeypatch.setattr(vision.requests, "post", post)
    seer = vision.VisionLLM("qwen2.5vl:7b")
    assert seer.see("look", ["aGk="]) == {"score": 6, "what": "a jump"}
    assert sent["url"].endswith("/api/chat") and sent["timeout"] == vision.TIMEOUT
    assert sent["body"]["options"]["num_ctx"] == vision.CONTEXT and sent["body"]["messages"][0]["images"] == ["aGk="]
    assert seer.loaded == 12.5
