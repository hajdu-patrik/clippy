import json
import re
import threading
import time

import pytest

from clippy import llm
from clippy.concurrency import GPU, GuardedLLM
from clippy.highlights import RERANK_SCHEMA, PickCache, find_highlights
from clippy.tools import log_sink

LESSON = "- hook type 'question': 2.0x the usual views (6 clips)"


class Picker:
    backend = "fake"
    local = True

    def __init__(self, parallel=2, delay=0.0):
        self.parallel, self.delay = parallel, delay
        self.calls, self.running, self.peak = [], 0, 0
        self.lock = threading.Lock()

    def describe(self):
        return "fake:model"

    def chat_json(self, system, user, think=False, schema=None):
        window = user.startswith("TRANSCRIPT:")
        with self.lock:
            self.calls.append((system, user, think))
            if window:
                self.running += 1
                self.peak = max(self.peak, self.running)
        try:
            time.sleep(self.delay if window else 0.0)
            if window:
                first = float(re.search(r"\[(\d+\.\d+)-", user).group(1))
                return {"clips": [{"start": first, "end": first + 12.0, "score": 70, "hook": "h",
                                   "hook_type": "question", "title": "T", "description": "d", "hashtags": [],
                                   "risks": [], "reason": "r"}]}
            if schema is RERANK_SCHEMA:
                return {"clips": [{"id": 1, "score": 90, "hook": "h", "title": "t"}]}
            return {}
        finally:
            if window:
                with self.lock:
                    self.running -= 1

    def windows(self):
        return [c for c in self.calls if c[1].startswith("TRANSCRIPT:")]

    def reranks(self):
        return [c for c in self.calls if c[0].startswith("You are the head editor")]


def transcript(seconds=200):
    words = [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4}
             for i, w in enumerate(("We", "start", "the", "heist", "now.") * int(seconds / 2.5))]
    return {"segments": [{"words": words}], "language": "en", "duration": float(seconds)}


def pick(model, **kwargs):
    return find_highlights(transcript(), model, max_clips=2, min_len=10, max_len=30, window_sec=40, overlap_sec=5,
                           title="Heist", **kwargs)


@pytest.fixture
def gpu_holds(monkeypatch):
    taken, real = [], GPU.hold
    monkeypatch.setattr(GPU, "hold", lambda: (taken.append(1), real())[1])
    return taken


@pytest.mark.parametrize("value, windows, rerank", [
    ("all", True, True), ("1", True, True), ("windows", True, False), ("rerank", False, True), ("off", False, False),
    ("0", False, False), (" Rerank ", False, True)])
def test_the_reasoning_runs_where_the_setting_says(monkeypatch, value, windows, rerank):
    monkeypatch.setenv("CLIPPY_LLM_THINK", value)
    assert (llm.thinks("windows"), llm.thinks("rerank")) == (windows, rerank)


def test_an_unknown_reasoning_setting_means_the_default(monkeypatch):
    monkeypatch.setenv("CLIPPY_LLM_THINK", "sometimes")
    assert llm.think_mode() == llm.DEFAULT_THINK
    monkeypatch.delenv("CLIPPY_LLM_THINK")
    assert llm.think_mode() == llm.DEFAULT_THINK
    monkeypatch.setenv("CLIPPY_LLM_THINK_BUDGET", "lots")
    monkeypatch.setenv("CLIPPY_LLM_PARALLEL", "0")
    assert llm.think_budget() == llm.THINK_BUDGET and llm.parallel_requests() == 1


def test_windows_are_asked_side_by_side_under_one_hold_of_the_gpu(monkeypatch, gpu_holds):
    monkeypatch.setenv("CLIPPY_LLM_THINK", "windows")
    picker, reports = Picker(parallel=2, delay=0.05), []
    with log_sink(lambda line: None, reports.append):
        clips = pick(GuardedLLM(picker))
    windows = picker.windows()
    assert len(windows) >= 5 and picker.peak == 2 and clips
    assert all(think for _, _, think in windows) and not any(think for _, _, think in picker.reranks())
    assert len(gpu_holds) == 1 + len(picker.calls) - len(windows)
    assert reports == sorted(reports) and reports[-1] == 1.0
    assert any(abs(r - 0.8) < 1e-9 for r in reports)


def test_one_request_at_a_time_when_the_server_serves_one(monkeypatch):
    monkeypatch.setenv("CLIPPY_LLM_PARALLEL", "1")
    model = llm.OllamaLLM(llm.DEFAULT_MODEL, llm.OLLAMA_URL)
    assert model.parallel == 1 and GuardedLLM(model).parallel == 1
    picker = Picker(parallel=1, delay=0.02)
    pick(picker)
    assert picker.peak == 1


def test_the_candidates_keep_the_window_order_whichever_answer_comes_first():
    class Uneven(Picker):
        def chat_json(self, system, user, think=False, schema=None):
            if user.startswith("TRANSCRIPT:[0.00"):
                time.sleep(0.1)
            return super().chat_json(system, user, think, schema)

    fast, slow = Picker(parallel=1), Uneven(parallel=3)
    assert [c.start for c in pick(fast)] == [c.start for c in pick(slow)]


def test_a_new_lesson_keeps_the_cached_window_answers_and_reaches_the_rerank(tmp_path, gpu_holds):
    cache = PickCache(tmp_path, "vid")
    pick(Picker(), cache=cache)
    learned = Picker()
    pick(GuardedLLM(learned), cache=cache, lessons=LESSON)
    assert not learned.windows() and LESSON in learned.reranks()[0][0]
    assert len(gpu_holds) == len(learned.calls)
    wished = Picker()
    pick(wished, cache=cache, lessons=LESSON, instructions="only the funny parts")
    assert wished.windows() and all(LESSON not in system for system, _, _ in wished.windows())


def test_an_answer_with_reasoning_is_cached_apart_from_one_without(tmp_path, monkeypatch):
    cache = PickCache(tmp_path, "vid")
    pick(Picker(), cache=cache)
    monkeypatch.setenv("CLIPPY_LLM_THINK", "off" if llm.thinks("windows") else "all")
    other = Picker()
    pick(other, cache=cache)
    assert other.windows()


def test_the_rerank_sees_each_hook_type():
    picker = Picker()
    pick(picker)
    assert "hook (question): h" in picker.reranks()[0][1]


class Stream:
    ok, status_code = True, 200

    def __init__(self, chunks):
        self.chunks, self.read = chunks, 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_lines(self):
        for chunk in self.chunks:
            self.read += 1
            yield json.dumps(chunk).encode()


class Reply:
    ok, status_code, text = True, 200, ""

    def json(self):
        return {"message": {"content": '{"clips": []}'}}


def thinking_server(monkeypatch, steps):
    sent, streams = [], []

    def post(url, json=None, timeout=None, stream=False):
        sent.append(json)
        if json["stream"]:
            streams.append(Stream([{"message": {"thinking": "step "}}] * steps +
                                  [{"message": {"content": '{"clips": [1]}'}, "done": True}]))
            return streams[-1]
        return Reply()

    monkeypatch.setattr(llm.requests, "post", post)
    return sent, streams


def test_reasoning_past_its_budget_is_cut_and_answered_from_its_notes(monkeypatch):
    monkeypatch.setenv("CLIPPY_LLM_THINK_BUDGET", "3")
    sent, streams = thinking_server(monkeypatch, steps=10)
    reply = llm.OllamaLLM(llm.DEFAULT_MODEL, llm.OLLAMA_URL).chat_json("sys", "question", think=True)
    first, second = sent
    assert first["think"] and first["options"]["num_predict"] == 3 + llm.ANSWER_TOKENS
    assert streams[0].read == 4 and reply == {"clips": []}
    assert second["think"] is False and not second["stream"]
    assert second["messages"][1]["content"] == "question" + llm.NOTES_RULE + "step " * 4


def test_reasoning_within_its_budget_is_the_answer(monkeypatch):
    monkeypatch.setenv("CLIPPY_LLM_THINK_BUDGET", "50")
    sent, _ = thinking_server(monkeypatch, steps=10)
    assert llm.OllamaLLM(llm.DEFAULT_MODEL, llm.OLLAMA_URL).chat_json("sys", "q", think=True) == {"clips": [1]}
    assert len(sent) == 1


def test_without_a_budget_or_reasoning_it_is_one_plain_request(monkeypatch):
    monkeypatch.setenv("CLIPPY_LLM_THINK_BUDGET", "0")
    sent, _ = thinking_server(monkeypatch, steps=10)
    model = llm.OllamaLLM(llm.DEFAULT_MODEL, llm.OLLAMA_URL)
    model.chat_json("sys", "q", think=True)
    monkeypatch.setenv("CLIPPY_LLM_THINK_BUDGET", "100")
    model.chat_json("sys", "q", think=False)
    assert [(s["stream"], s["think"], s["options"]["num_predict"]) for s in sent] == [
        (False, True, 8192), (False, False, llm.ANSWER_TOKENS)]
