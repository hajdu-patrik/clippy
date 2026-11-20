import threading

from clippy.tools import log, log_sink, report, report_span
from clippy.web.db import DB
from clippy.web.hub import Hub
from clippy.web.progress import STAGES, Progress


def make(tmp_path, stages=tuple(st.key for st in STAGES)):
    db = DB(tmp_path / "p.db")
    chat = db.create_chat("t")
    return Progress(db, Hub(), chat["id"], {"key": "job.generate", "params": {"n": 2}}, stages), db


def test_report_spans_nest_and_route_to_the_thread_sink():
    seen = []
    with log_sink(lambda line: None, seen.append):
        with report_span(0.5, 1.0):
            with report_span(0.0, 0.5):
                report(0.5)
    assert 0.625 in seen
    assert seen[-1] == 1.0
    report(0.3)
    other = []
    t = threading.Thread(target=lambda: report(0.9))
    with log_sink(lambda line: None, other.append):
        t.start()
        t.join()
    assert other == []


def test_queue_position_then_stages_advance_and_cached_ones_drop_out(tmp_path):
    p, _ = make(tmp_path)
    p.queued(2)
    assert p.data(False)["phase"] == "queued" and p.data(False)["queue_position"] == 2
    p.begin()
    with log_sink(p.line, p.report):
        log("[download] already here: x.mkv")
        log("[audio] voice/music separation: cache")
        log("[transcribe] large-v3, cuda – starting…")
        report(0.5)
    d = p.data(False)
    states = {s["key"]: s["state"] for s in d["stages"]}
    assert states["download"] == "cached" and states["audio"] == "cached" and states["transcribe"] == "active"
    live_weight = sum(p.weight[st.key] for st in STAGES if st.key not in ("download", "audio"))
    assert abs(d["percent"] - round(100 * p.weight["transcribe"] * 0.5 / live_weight, 1)) < 0.2
    assert d["stage"] == "Transcript"


def test_lines_from_earlier_stages_do_not_move_backwards_and_render_counts_clips(tmp_path):
    p, _ = make(tmp_path)
    p.begin()
    for line in ("[download] Title (10 min)", "[hook] window 2/5  (1:00–2:00)", "[render] 2/4  #2",
                 "[audio] loudness measurement failed, single-pass normalisation"):
        p.line(line)
    d = p.data(False)
    states = {s["key"]: s["state"] for s in d["stages"]}
    assert states["render"] == "active" and states["select"] == "done"
    assert states["audio"] == "skipped" and states["download"] == "done"
    assert d["detail"] == "Clip 2 of 4"


def test_finish_marks_everything_and_errors_flag_the_failed_step(tmp_path):
    p, _ = make(tmp_path, ("select", "render"))
    p.begin()
    p.finish({"key": "done.more", "params": {"n": 2}})
    d = p.data(True)
    assert d["percent"] == 100 and {s["state"] for s in d["stages"]} <= {"done", "skipped"}

    q, _ = make(tmp_path, ("select", "render"))
    q.begin()
    q.line("[hook] window 1/3")
    q.finish({"key": "error.plain", "params": {"error": "boom"}}, error=True)
    assert {s["key"]: s["state"] for s in q.data(True)["stages"]}["select"] == "failed"


def test_eta_appears_once_there_is_measurable_progress(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("clippy.web.progress.time.time", lambda: clock[0])
    p, _ = make(tmp_path, ("transcribe", "render"))
    p.begin()
    assert p.eta is None
    clock[0] += 30
    p.report(0.25)
    assert p.eta is not None and 60 < p.eta < 400


def test_render_weight_scales_with_the_clip_count(tmp_path):
    p, _ = make(tmp_path, ("select", "render"))
    assert p.weight["render"] == 5
    p.begin()
    p.line("[render] 1/4  #3")
    assert p.weight["render"] == 20


def test_each_stage_keeps_its_own_time_and_the_log_gets_a_summary(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("clippy.web.progress.time.time", lambda: now[0])
    lines = []
    monkeypatch.setattr("clippy.web.progress.log", lines.append)
    p, db = make(tmp_path, ("select", "render"))
    p.begin()
    now[0] = 130.0
    p.line("[render] 1/1  #1")
    now[0] = 175.0
    p.finish({"key": "done.more", "params": {"n": 1}})
    stages = {s["key"]: s.get("seconds") for s in db.get_message(p.message["id"])["data"]["stages"]}
    assert stages == {"select": 30.0, "render": 45.0}
    assert lines[-1] == "[timing] select 30 s, render 45 s"
