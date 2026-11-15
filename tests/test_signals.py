import json
import subprocess
import wave

import numpy as np
import pytest

from clippy import signals
from clippy.highlights import PEAK_REASON, find_highlights
from clippy.signals import Signals
from clippy.tools import ClippyError, ffmpeg_path


def write_wav(path, seconds=120, burst=(60.0, 61.5), rate=8000):
    rng = np.random.default_rng(1)
    audio = 0.02 * rng.standard_normal(seconds * rate)
    a, b = int(burst[0] * rate), int(burst[1] * rate)
    audio[a:b] = 0.7 * np.sin(np.arange(b - a) * 2 * np.pi * 440 / rate)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes((audio * 32767).astype(np.int16).tobytes())
    return path


def test_a_loud_moment_stands_out_from_its_surroundings(tmp_path):
    found = signals.build(write_wav(tmp_path / "s.wav"), [])
    assert len(found.loud) == 240
    [peak] = found.peaks()
    assert 59.0 <= peak.time <= 62.0 and peak.kinds == ("loud",) and peak.strength > 0.6


def test_rises_ignore_a_slowly_changing_level():
    level = np.linspace(-40, -20, 600)
    assert signals.rises(level).max() == 0.0
    level[300] += 25
    assert signals.rises(level)[300] == 1.0


def test_laughter_comes_from_the_transcript():
    words = [{"word": w, "start": t, "end": t + 0.3} for t, w in
             ((1.0, "so"), (5.0, "hahaha"), (9.0, "(laughs)"), (12.0, "lol"), (15.0, "nevetett"), (20.0, "harbor"))]
    assert signals.build(None, words).laughs == [5.0, 9.0, 12.0, 15.0]


def chat_line(seconds, text):
    return json.dumps({"replayChatItemAction": {"videoOffsetTimeMsec": str(int(seconds * 1000)), "actions": [
        {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {"message": {"runs": [
            {"text": text}, {"emoji": {"shortcuts": [":fire:"]}}]}}}}}]}})


def test_a_chat_burst_is_a_peak_with_what_the_chat_wrote(tmp_path):
    lines = [chat_line(0, "when does it start")] * 30 + [chat_line(t, "nice") for t in range(10, 600, 10)]
    lines += [chat_line(300 + i * 0.2, "OMG") for i in range(25)] + [chat_line(301, "clip it")]
    lines.append("not json")
    path = tmp_path / "v.live_chat.json"
    path.write_text("\n".join(lines), encoding="utf-8")
    assert signals.chat_messages(path)[0] == (10.0, "nice:fire:")
    found = signals.build(None, [], chat=path)
    peak = found.peaks()[0]
    assert 298 <= peak.time <= 306 and "chat" in peak.kinds
    text = found.describe(295, 310)
    assert text.startswith("chat x") and '"OMG:fire:"' in text
    again = Signals.from_json(json.loads(json.dumps(found.to_json())))
    assert again.peaks() == found.peaks() and again.chat_lines[0] == (10.0, "nice:fire:")


def test_sudden_action_on_screen_is_read_from_the_keyframes(tmp_path):
    video = tmp_path / "v.mp4"
    graph = "color=c=gray:s=160x90:r=10:d=60,geq=lum='if(between(T,30,31),255*random(1),128)':cb=128:cr=128"
    subprocess.run([ffmpeg_path(), "-loglevel", "error", "-f", "lavfi", "-i", graph, "-c:v", "libx264", "-g", "5",
                    "-pix_fmt", "yuv420p", str(video)], check=True)
    moves = signals.keyframe_motion(video)
    assert len(moves) > 50
    found = signals.build(None, [], video=video)
    assert any(29 <= p.time <= 32.5 and "action" in p.kinds for p in found.peaks())


class PickerLLM:
    """Proposes one spoken moment; writes texts for reaction clips."""

    backend = "ollama"

    def __init__(self):
        self.prompts = []

    def describe(self):
        return "fake"

    def chat_json(self, system, prompt, **kwargs):
        self.prompts.append((system, prompt))
        schema = kwargs.get("schema") or {}
        fields = schema.get("properties", {}).get("clips", {}).get("items", {}).get("properties", {})
        if prompt.startswith("TRANSCRIPT:"):
            return {"clips": [{"start": 0.0, "end": 12.0, "score": 70, "hook": "h", "hook_type": "question",
                               "title": "Talk", "description": "d", "hashtags": [], "risks": [], "reason": "r"}]}
        if "description" in fields:
            return {"clips": [{"id": 0, "title": "The explosion nobody saw coming", "description": "Boom 💥",
                               "question": "Did you see it?", "topic": "GTA 6"}]}
        return {}


def speech(until=14.0):
    return [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4}
            for i, w in enumerate(("We", "start", "the", "heist", "now.") * int(until / 2.5))]


def loud_at(times, length=400):
    loud = [0.0] * length
    for t in times:
        loud[int(t / 0.5)] = 1.0
    return Signals(loud=loud)


def test_reactions_mark_the_transcript_and_add_moments_without_speech():
    llm = PickerLLM()
    transcript = {"segments": [{"words": speech()}], "language": "en", "duration": 200.0}
    clips = find_highlights(transcript, llm, max_clips=2, min_len=10, max_len=30, title="GTA 6 heist",
                            signals=loud_at([5.0, 150.0]))
    system, window = llm.prompts[0]
    assert "⟪loud⟫" in window and "reacted" in system
    quiet = [c for c in clips if c.start > 100]
    assert quiet and quiet[0].reason.startswith(PEAK_REASON) and quiet[0].start < 150.0 < quiet[0].end
    assert quiet[0].title == "The explosion nobody saw coming" and quiet[0].topic == "GTA 6"
    assert any("REACTIONS: loud" in prompt and "(no speech)" in prompt for _, prompt in llm.prompts)


def test_a_long_video_without_speech_is_picked_by_its_reactions():
    transcript = {"segments": [], "language": "en", "duration": 200.0}
    [clip] = find_highlights(transcript, PickerLLM(), max_clips=1, min_len=10, max_len=30, title="Stream",
                             signals=loud_at([120.0]))
    assert clip.start < 120.0 < clip.end and clip.duration == 20.0
    with pytest.raises(ClippyError) as caught:
        find_highlights(transcript, PickerLLM(), max_clips=1, signals=Signals(loud=[0.0] * 400))
    assert caught.value.key == "error.empty_transcript"
