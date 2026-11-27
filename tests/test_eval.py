import json
import shutil
from pathlib import Path

import pytest

from clippy import control, evaluation, highlights, shell
from clippy.boundaries import Boundaries
from clippy.cli import eval_main
from clippy.highlights import Clip
from clippy.storage import Storage
from clippy.tools import ClippyError, read_json, write_json

FIXTURE = Path(__file__).parent / "fixtures" / "eval"
TRANSCRIPT = "talk.0123456789.aligned.json"


def sentence(k: int) -> tuple[float, float]:
    """Start and end of the fixture's sentence k (12 words, 0.5 s apart, 0.3 s between sentences)."""
    return round(6.3 * k, 2), round(6.3 * k + 5.9, 2)


@pytest.fixture
def storage(tmp_path):
    st = Storage(data=tmp_path / "data")
    shutil.copy(FIXTURE / TRANSCRIPT, st.cache("transcripts") / TRANSCRIPT)
    shutil.copy(FIXTURE / "golden.json", st.golden_file)
    return st


class FakeLLM:
    """Proposes a labelled moment first and an unlabelled one second; every check answers "ok"."""

    backend = "ollama"

    def __init__(self, spans=((3, 5), (18, 20))):
        self.spans, self.calls = spans, []

    def describe(self):
        return "fake:picker"

    def chat_json(self, system, user, think=False, schema=None):
        self.calls.append(evaluation.step_of(system, schema))
        if schema is highlights.CLIPS_SCHEMA:
            return {"clips": [{"start": sentence(a)[0], "end": sentence(b)[1], "score": 90 - 10 * i, "hook": "h",
                               "title": f"t{i}", "description": "d", "hashtags": [], "risks": [], "reason": "r"}
                              for i, (a, b) in enumerate(self.spans)]}
        return {"clips": [], "hooks": []}


def test_the_golden_fixture_scores_recall_precision_and_edges(storage):
    llm = FakeLLM()
    report = evaluation.run(storage.golden_file, storage, llm)
    video = report["videos"][0]
    assert [(round(p["start"], 1), round(p["end"], 1)) for p in video["picks"]] == [(18.7, 37.7), (113.2, 132.2)]
    assert video["picks"][0]["labels"] == [0] and video["picks"][1]["labels"] == []
    m = video["metrics"]
    assert m["recall@1"] == m["recall@5"] == m["recall"] == 0.5 and m["precision"] == 0.5
    assert m["recall_graded"] == 0.75
    assert m["start_ok"] == m["end_ok"] == 1.0 and m["mid_cut"] == 0.0
    assert video["llm_calls"] == len(llm.calls) and video["steps"]["windows"]["calls"] == 1
    assert set(video["steps"]) == {"windows", "starts", "ends", "local"}
    assert report["summary"]["metrics"] == m and report["llm"] == "fake:picker"


def test_llm_calls_are_told_apart_by_the_step_that_makes_them():
    assert evaluation.step_of("x", highlights.CLIPS_SCHEMA) == "windows"
    assert evaluation.step_of("x", highlights.RERANK_SCHEMA) == "rerank"
    assert evaluation.step_of(highlights.STARTS_PROMPT, highlights.EDGES_SCHEMA) == "starts"
    assert evaluation.step_of(highlights.ENDINGS_PROMPT, highlights.EDGES_SCHEMA) == "ends"
    assert evaluation.step_of("x", highlights.POLISH_SCHEMA) == "polish"
    assert evaluation.step_of("x", None) == "other"

    class Broken:
        def chat_json(self, *a, **k):
            raise RuntimeError("model gone")

    meter = evaluation.MeteredLLM(Broken())
    with pytest.raises(RuntimeError):
        meter.chat_json("x", "y", schema=highlights.RERANK_SCHEMA)
    assert meter.steps["rerank"]["calls"] == 1 and meter.steps["rerank"]["failed"] == 1


def test_a_cut_inside_a_sentence_or_a_word_is_a_mid_cut():
    words = [w for s in read_json(FIXTURE / TRANSCRIPT)["segments"] for w in s["words"]]
    bounds = Boundaries(words)
    whole = evaluation.edges(Clip(sentence(2)[0], sentence(4)[1] + 0.1, 50), bounds)
    assert whole == {"start_ok": True, "end_ok": True, "mid_cut": False}
    inside = evaluation.edges(Clip(sentence(2)[0] + 1.0, sentence(4)[1] - 1.0, 50), bounds)
    assert inside == {"start_ok": False, "end_ok": False, "mid_cut": True}
    in_a_word = evaluation.edges(Clip(sentence(2)[0], sentence(4)[1] + 0.6, 50), bounds)
    assert in_a_word["end_ok"] and in_a_word["mid_cut"]
    assert evaluation.edges(Clip(500.0, 520.0, 50), bounds) is None


def test_recall_counts_only_the_first_k_picks_and_grades_weigh_in():
    moments = [{"start": 0, "end": 10, "grade": 3}, {"start": 50, "end": 60}, {"start": 100, "end": 110, "grade": 7}]
    picks = [Clip(200, 210, 90), Clip(52, 62, 80), Clip(0, 30, 70), Clip(104, 140, 60)]
    counts = evaluation.score(picks, moments, None)
    rates = evaluation.rates(counts)
    assert rates["recall@1"] == 0.0 and rates["recall@3"] == round(2 / 3, 4) and rates["recall@5"] == 1.0
    assert rates["precision"] == 0.75 and rates["recall_graded"] == 1.0 and rates["start_ok"] is None
    assert counts["graded"] == 3 + 2 + 2
    assert evaluation.rates(evaluation.score(picks, [], None))["precision"] is None


def test_bootstrap_makes_labels_from_the_kept_clips_and_never_touches_edited_ones(tmp_path):
    st = Storage(data=tmp_path / "data")
    shutil.copy(FIXTURE / TRANSCRIPT, st.cache("transcripts") / TRANSCRIPT)
    write_json(st.cache("signals") / "talk.v.json", {"hop": 0.5})
    write_json(st.project("talk") / "clips.json", {"source": "C:/videos/talk.mp4", "title": "Talk", "clips": [
        Clip(18.9, 37.4, 90, title="Stories").to_dict(), Clip(75.6, 94.1, 80, hook="More").to_dict()]})
    write_json(st.project("gone") / "clips.json", {"source": "https://example.com/v/gone", "clips": []})
    golden = st.golden_file
    done = evaluation.bootstrap(st, golden)
    assert (done["added"], done["kept"], done["missing"]) == (1, 0, ["gone"])
    video = read_json(golden)["videos"][0]
    assert video["id"] == "talk" and video["transcript"] == TRANSCRIPT and video["signals"] == "talk.v.json"
    assert [(m["start"], m["end"], m["grade"], m["note"]) for m in video["moments"]] == [
        (18.9, 37.4, 2, "Stories"), (75.6, 94.1, 2, "More")]
    edited = read_json(golden)
    edited["videos"][0]["moments"] = edited["videos"][0]["moments"][:1]
    write_json(golden, edited)
    assert evaluation.bootstrap(st, golden)["kept"] == 1
    assert len(read_json(golden)["videos"][0]["moments"]) == 1


def test_a_broken_golden_set_or_report_says_what_is_wrong(tmp_path):
    with pytest.raises(ClippyError) as missing:
        evaluation.load_golden(tmp_path / "none.json")
    assert "clippy eval --bootstrap" in str(missing.value)
    bad = tmp_path / "bad.json"
    write_json(bad, {"videos": [{"id": "v", "transcript": "t", "moments": [{"start": 9, "end": 3}]}]})
    with pytest.raises(ClippyError) as broken:
        evaluation.load_golden(bad)
    assert "ends before it starts" in str(broken.value)
    with pytest.raises(ClippyError):
        evaluation.load_report(bad)


def test_a_video_without_its_cached_transcript_is_skipped(storage):
    golden = read_json(storage.golden_file)
    golden["videos"].append({**golden["videos"][0], "id": "lost", "transcript": "lost.x.aligned.json"})
    write_json(storage.golden_file, golden)
    report = evaluation.run(storage.golden_file, storage, FakeLLM())
    assert report["summary"]["videos"] == 1 and report["summary"]["skipped"] == 1
    assert "lost" in evaluation.table(report)
    with pytest.raises(ClippyError):
        evaluation.run(storage.golden_file, storage, FakeLLM(), only=["nope"])


def test_clippy_eval_writes_a_report_prints_a_table_and_compares_two(storage, capsys):
    a, b = storage.data / "a.json", storage.data / "b.json"
    assert eval_main(["--data", str(storage.data), "--llm", "none", "--out", str(a)]) == 0
    out = capsys.readouterr().out
    assert "R@1" in out and "talk" in out and "without LLM" in out
    report = read_json(a)
    report["summary"]["metrics"]["recall@1"] = 0.0
    report["summary"]["seconds"] += 10
    write_json(b, {**report, "llm": "other"})
    assert control.main(["eval", "--compare", str(a), str(b)]) == 0
    table = capsys.readouterr().out
    assert "recall@1" in table and "other" in table and "+10.0" in table
    assert json.loads(a.read_text(encoding="utf-8"))["format"] == evaluation.FORMAT


def test_the_eval_flags_are_in_both_completion_scripts():
    ps, lua = shell.powershell_script(), shell.clink_script()
    flags = [f for f, _, _ in shell.flags_of("eval")]
    assert {"--bootstrap", "--compare", "--golden", "--video", "--llm"} <= set(flags)
    for f in flags:
        assert f"'{f}'" in ps and f'"{f}"' in lua


def test_a_failed_picker_keeps_its_failed_calls_in_the_summary(storage):
    class Down(FakeLLM):
        def chat_json(self, system, user, think=False, schema=None):
            raise ClippyError("The language model did not answer")

    report = evaluation.run(storage.golden_file, storage, Down())
    assert "did not answer" in report["videos"][0]["error"] and report["summary"]["skipped"] == 1
    assert report["summary"]["steps"]["windows"] == {"calls": 1, "seconds": report["summary"]["steps"]["windows"]["seconds"],
                                                     "failed": 1}
