import json

from clippy import download, signals
from clippy.download import Remote, keep_meta, meta_file, read_meta
from clippy.highlights import PEAK_REASON, REPLAYED_RULE, _windows, find_highlights
from clippy.signals import Signals

PODCAST = [0.31, 0.33, 0.11, 0.05, 0.07, 0.12, 0.21, 0.33, 0.02, 0.09, 0.09, 0.15, 0.09, 0.1, 0.13, 0.16, 0.05, 0.14,
           0.09, 0.05, 0.06, 0.07, 0.15, 0.16, 1.0, 0.04, 0.03, 0.04, 0.03, 0.03, 0.05, 0.06, 0.04, 0.01, 0.03, 0.03,
           0.02, 0.01, 0.1, 0.1, 0.06, 0.09, 0.14, 0.29, 0.92, 0.08, 0.12, 0.23, 0.08, 0.07, 0.16, 0.27, 0.05, 0.09,
           0.1, 0.13, 0.02, 0.05, 0.03, 0.05, 0.75, 0.08, 0.04, 0.07, 0.06, 0.16, 0.17, 0.18, 0.34, 0.05, 0.1, 0.17,
           0.09, 0.66, 0.08, 0.09, 0.11, 0.08, 0.1, 0.27, 0.14, 0.07, 0.07, 0.07, 0.21, 0.8, 0.05, 0.06, 0.1, 0.02,
           0.2, 0.06, 0.04, 0.03, 0.0, 0.05, 0.07, 0.04, 0.11, 0.09]
PODCAST_STEP = 26.76
PODCAST_CHAPTERS = [0, 25, 471, 854, 1030, 1183, 1378, 1626, 1755, 1974, 2282, 2423, 2568]
AD_BREAKS = [0.2, 0.1, 0.28, 0.01, 0.0, 0.0, 0.04, 0.02, 0.05, 0.75, 0.27, 0.15, 0.14, 0.16, 0.39, 0.11, 0.23, 0.24,
             0.74, 0.14, 0.13, 0.61, 0.18, 0.37, 0.14, 0.21, 0.18, 0.33, 1.0, 0.11, 0.1, 0.06, 0.05, 0.05, 0.79, 0.13,
             0.1, 0.11, 0.1, 0.24, 0.65, 0.06, 0.1, 0.1, 0.05, 0.07, 0.5, 0.09, 0.13, 0.11, 0.24, 0.62, 0.04, 0.04,
             0.06, 0.1, 0.44, 0.09, 0.1, 0.14, 0.15, 0.67, 0.17, 0.17, 0.32, 0.16, 0.11, 0.58, 0.59, 0.17, 0.15, 0.16,
             0.23, 0.98, 0.34, 0.25, 0.23, 0.3, 0.21, 0.66, 0.27, 0.2, 0.25, 0.18, 0.58, 0.33, 0.39, 0.36, 0.15, 0.47,
             0.05, 0.07, 0.07, 0.49, 0.07, 0.07, 0.06, 0.03, 0.01, 0.01]


def heatmap(values, step):
    return [{"start_time": round(i * step, 3), "end_time": round((i + 1) * step, 3), "value": v}
            for i, v in enumerate(values)]


def chapters(starts, end):
    return [{"start_time": a, "end_time": b, "title": f"part {i}"} for i, (a, b) in
            enumerate(zip(starts, starts[1:] + [end]))]


def test_the_replayed_parts_stand_out_from_the_videos_own_curve():
    found = signals.replays(heatmap(PODCAST, PODCAST_STEP), duration=2676)
    strong = [round(a) for a, _, x in found if x >= signals.TAG_LEVEL]
    assert strong == [187, 642, 1151, 1177, 1365, 1606, 1820, 1953, 2114, 2275]
    assert all(a >= 90 for a, _, _ in found)


def test_a_spike_where_a_chapter_starts_is_a_jump_not_a_replay():
    found = signals.replays(heatmap(PODCAST, PODCAST_STEP), chapters(PODCAST_CHAPTERS, 2676), 2676)
    assert [round(a) for a, _, x in found if x >= signals.TAG_LEVEL] == [187, 642, 1151, 1820, 2114]


def test_spikes_on_an_even_beat_are_ad_breaks_and_the_curve_is_dropped():
    assert signals.replays(heatmap(AD_BREAKS, 33.97), duration=3397) == []
    assert signals.replays([], duration=600) == [] and signals.replays(heatmap([0.5] * 5, 10)) == []
    assert signals.replays([{"start_time": "x"}] * 40) == []


def test_a_replayed_part_marks_its_lines_and_peaks_in_its_middle():
    spans = [(100.0, 140.0, 1.0), (300.0, 340.0, 0.2)]
    found = Signals(loud=[0.0] * 800, replayed=signals.replay_curve(spans, 800))
    [peak] = found.peaks()
    assert 118 <= peak.time <= 122 and peak.kinds == ("replayed",)
    assert found.tags(101.0, 104.0) == "replayed" and found.tags(301.0, 305.0) == ""
    assert found.has("replayed") and not found.has("loud")
    assert signals.replay_curve([], 800) == []


def test_the_replayed_parts_are_laid_on_the_cache_and_never_stored_in_it(tmp_path):
    path = tmp_path / "vid.json"
    plain = signals.cached(path, lambda: Signals(loud=[0.0] * 400))
    stored = path.read_text(encoding="utf-8")
    assert plain.replayed == [] and plain.peaks() == []
    again = signals.cached(path, lambda: None, [(60.0, 90.0, 0.9)])
    assert again.peaks()[0].kinds == ("replayed",) and path.read_text(encoding="utf-8") == stored
    assert json.loads(stored)["replayed"] == []


class Picker:
    backend = "ollama"

    def __init__(self):
        self.prompts = []

    def describe(self):
        return "fake"

    def chat_json(self, system, prompt, **kwargs):
        self.prompts.append((system, prompt))
        if prompt.startswith("TRANSCRIPT:"):
            return {"clips": [{"start": 0.0, "end": 12.0, "score": 70, "hook": "h", "hook_type": "question",
                               "title": "Talk", "description": "d", "hashtags": [], "risks": [], "reason": "r"}]}
        return {}


def talk(until):
    return [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4}
            for i, w in enumerate(("We", "talk", "about", "the", "show.") * int(until / 2.5))]


def test_the_picker_is_told_about_the_replayed_parts_and_gets_one_as_a_candidate():
    llm = Picker()
    transcript = {"segments": [{"words": talk(200.0)}], "language": "en", "duration": 200.0}
    replayed = Signals(loud=[0.0] * 400, replayed=signals.replay_curve([(140.0, 160.0, 1.0)], 400))
    clips = find_highlights(transcript, llm, max_clips=2, min_len=10, max_len=30, title="Show", signals=replayed)
    system, window = llm.prompts[0]
    assert "⟪replayed⟫" in window and REPLAYED_RULE in system
    assert any(c.reason.startswith(PEAK_REASON) and c.start <= 150.0 <= c.end for c in clips)
    plain = Picker()
    find_highlights(transcript, plain, max_clips=2, min_len=10, max_len=30, title="Show",
                    signals=Signals(loud=[1.0 if i == 200 else 0.0 for i in range(400)]))
    assert REPLAYED_RULE not in plain.prompts[0][0]


def test_a_chapter_start_ends_the_window_and_opens_the_next():
    segs = [{"start": i * 10.0, "end": i * 10.0 + 9, "text": "x"} for i in range(100)]
    assert _windows(segs, 300, 60, []) == _windows(segs, 300, 60)
    wins = _windows(segs, 300, 60, [100.0, 250.0, 700.0])
    assert [(w[0]["start"], w[-1]["start"]) for w in wins] == [(0, 240), (250, 540), (490, 690), (700, 990)]


def test_the_heatmap_and_chapters_are_kept_next_to_the_video(tmp_path):
    info = {"heatmap": heatmap([0.1, 0.9], 5) + [{"start_time": 1}], "chapters": [
        {"start_time": 0, "end_time": 10, "title": "Intro", "extra": 1}, {"title": "no time"}]}
    meta = keep_meta(Remote("https://youtu.be/x", info, "vid", "Video"), tmp_path)
    assert meta == {"heatmap": heatmap([0.1, 0.9], 5), "chapters": [{"start_time": 0, "end_time": 10, "title": "Intro"}]}
    assert keep_meta(Remote("https://youtu.be/x", {}, "vid", "Video"), tmp_path) == meta
    assert read_meta(tmp_path, "other") == {"heatmap": [], "chapters": []}
    meta_file(tmp_path, "bad").write_text("[1, 2]", encoding="utf-8")
    assert read_meta(tmp_path, "bad") == {"heatmap": [], "chapters": []}
    assert download.meta_file(tmp_path, "vid").name == "vid.meta.json"
