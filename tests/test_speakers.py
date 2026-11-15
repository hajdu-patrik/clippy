import wave

import numpy as np

from clippy import speakers
from clippy.highlights import find_highlights

RATE = 16000


def voice(seconds, pitch, bright, rng):
    t = np.arange(int(seconds * RATE)) / RATE
    wobble = 1 + 0.03 * np.sin(2 * np.pi * 5 * t)
    sound = sum((bright ** k) * np.sin(2 * np.pi * pitch * k * wobble * t + rng.uniform(0, 6)) for k in range(1, 12))
    return 0.2 * sound / np.abs(sound).max() + 0.003 * rng.standard_normal(len(t))


def two_voices(path, turns=16, seconds=3.0):
    rng = np.random.default_rng(3)
    parts, sentences = [], []
    for i in range(turns):
        low = i % 2 == 0
        parts.append(voice(seconds, 110 if low else 240, 0.45 if low else 0.85, rng))
        parts.append(np.zeros(int(0.3 * RATE)))
        start = i * (seconds + 0.3)
        sentences.append({"start": start, "end": start + seconds, "text": f"sentence {i}."})
    audio = np.concatenate(parts)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes((audio * 32767).astype(np.int16).tobytes())
    return sentences


def test_two_different_voices_are_told_apart(tmp_path):
    sentences = two_voices(tmp_path / "talk.wav")
    turns = speakers.speaker_turns(tmp_path / "talk.wav", sentences)
    assert turns == ["A", "B"] * 8


def test_one_voice_gets_no_labels(tmp_path):
    rng = np.random.default_rng(5)
    audio = np.concatenate([voice(3.0, 120, 0.5, rng) for _ in range(14)])
    with wave.open(str(tmp_path / "one.wav"), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes((audio * 32767).astype(np.int16).tobytes())
    sentences = [{"start": 3.0 * i, "end": 3.0 * i + 2.9, "text": "x."} for i in range(14)]
    assert set(speakers.speaker_turns(tmp_path / "one.wav", sentences)) == {""}


def test_the_picker_sees_who_speaks():
    seen = []

    class LLM:
        backend = "ollama"

        def describe(self):
            return "fake"

        def chat_json(self, system, prompt, **kwargs):
            seen.append((system, prompt))
            return {}

    words = [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4} for i, w in enumerate(["Why", "not?", "Because."] * 6)]
    find_highlights({"segments": [{"words": words}], "language": "en"}, LLM(), max_clips=1, min_len=2, max_len=9,
                    speakers=lambda sents: ["A" if i % 2 == 0 else "B" for i in range(len(sents))])
    system, window = seen[0]
    assert "told apart by their voices" in system and "] A: Why not?" in window and "] B: Because." in window
    find_highlights({"segments": [{"words": words}], "language": "en"}, LLM(), max_clips=1, min_len=2, max_len=9,
                    speakers=lambda sents: ["A"] * len(sents))
    assert "told apart" not in seen[-1][0] and "A: " not in seen[-1][1]
