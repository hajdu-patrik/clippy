import wave

import numpy as np
import pytest

from clippy import audio_events, signals
from clippy.signals import Signals


def write_wav(path, seconds=150, events=((40.0, 43.0), (100.0, 104.0)), rate=16000):
    rng = np.random.default_rng(2)
    audio = 0.01 * rng.standard_normal(seconds * rate)
    for amp, (a, b) in zip((0.5, 0.9), events):
        audio[int(a * rate):int(b * rate)] = amp * np.sign(np.sin(np.arange(int((b - a) * rate)) * 0.05))
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes((audio * 32767).astype(np.int16).tobytes())
    return path


class FakeYamnet:
    """Hears laughter where the sound is medium loud and applause where it is very loud, frame by frame."""

    def __init__(self):
        self.lengths = []

    def run(self, outputs, feeds):
        wave_in = feeds["waveform"]
        assert wave_in.dtype == np.float32 and wave_in.ndim == 1
        self.lengths.append(len(wave_in))
        step = int(audio_events.FRAME_HOP * audio_events.RATE)
        frames = max(1, len(wave_in) // step - 1)
        scores = np.zeros((frames, 521), np.float32)
        for k in range(frames):
            level = np.abs(wave_in[k * step:k * step + 2 * step]).max()
            if 0.3 < level < 0.7:
                scores[k, 13] = 0.9
            elif level >= 0.7:
                scores[k, 62] = 0.95
        return [scores, None, None]


@pytest.fixture
def fake_model(monkeypatch):
    model = FakeYamnet()
    monkeypatch.setattr(audio_events, "_ready", True)
    monkeypatch.setattr(audio_events, "_session", model)
    return model


def test_laughter_and_applause_are_heard_in_the_sound(tmp_path, fake_model):
    heard = audio_events.listen(write_wav(tmp_path / "m.wav"))
    assert len(fake_model.lengths) == 2, "two minute chunks, a long file never sits in memory"
    assert abs(len(heard.times) - 150 / 0.48) <= 3
    laughing = heard.times[heard.laugh > 0.5]
    clapping = heard.times[heard.applause > 0.5]
    assert 39.5 <= laughing.min() and laughing.max() <= 43.5
    assert 99.5 <= clapping.min() and clapping.max() <= 104.5
    assert heard.cheer.max() == 0.0


def test_heard_reactions_become_peaks_and_marks(tmp_path, fake_model):
    sound = write_wav(tmp_path / "m.wav")
    found = signals.build(sound, [], heard=(sound,))
    assert len(found.laughter) == len(found.loud) == 300
    kinds = {round(p.time / 10): p.kinds for p in found.peaks()}
    assert any("laughter" in k for t, k in kinds.items() if 4 <= t <= 4.4)
    assert any("applause" in k for t, k in kinds.items() if 10 <= t <= 10.4)
    assert "laughter" in found.tags(40.0, 42.0) and "applause" in found.tags(101.0, 103.0)
    assert found.tags(10.0, 20.0) == ""
    again = Signals.from_json(found.to_json())
    assert again.peaks() == found.peaks()


def test_without_the_model_laughter_comes_from_the_words_only(tmp_path):
    words = [{"word": "hahaha", "start": 5.0, "end": 5.4}]
    sound = write_wav(tmp_path / "m.wav")
    assert audio_events.ready() is False
    with pytest.raises(RuntimeError):
        audio_events.listen(sound)
    found = signals.build(sound, words)
    assert found.laughs == [5.0] and found.laughter == [] and "laughter" in found.kinds(4.0, 6.0)


def test_a_model_with_another_checksum_is_never_loaded(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_events, "_ready", None)
    monkeypatch.setattr(audio_events, "YAMNET_PATH", tmp_path / "yamnet.onnx")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            yield b"not the model"

    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: Response())
    assert audio_events.ready() is False
    assert not (tmp_path / "yamnet.onnx").exists()


def test_old_reactions_without_heard_sounds_still_load():
    old = {"hop": 0.5, "loud": [0.0, 1.0, 0.0], "laughs": [0.5], "chat_lines": []}
    found = Signals.from_json(old)
    assert found.laughter == [] and found.kinds(0.0, 1.0) == ("loud", "laughter")


def test_the_voice_hears_the_laughter_and_the_music_stem_the_applause(tmp_path, fake_model):
    voice = write_wav(tmp_path / "vocals.wav", events=((40.0, 43.0),))
    music = write_wav(tmp_path / "music.wav", events=((0.0, 0.0), (100.0, 104.0)))
    found = signals.build(voice, [], heard=(voice, music))
    assert len(fake_model.lengths) == 4
    assert found.tags(40.0, 42.0) == "loud, laughter" and "applause" in found.tags(101.0, 103.0)


def test_a_model_that_fails_midway_leaves_the_other_reactions(tmp_path, fake_model, monkeypatch):
    def broken(*a):
        raise RuntimeError("onnx says no")

    monkeypatch.setattr(fake_model, "run", broken)
    sound = write_wav(tmp_path / "m.wav")
    found = signals.build(sound, [{"word": "haha", "start": 5.0, "end": 5.3}], heard=(sound,))
    assert len(found.loud) == 300 and found.laughter == [] and found.laughs == [5.0]


def test_an_odd_byte_from_the_pipe_does_not_shift_the_samples(tmp_path, fake_model, monkeypatch):
    import io
    import subprocess

    real = subprocess.Popen

    class Dribble(io.RawIOBase):
        def __init__(self, inner):
            self.inner = inner

        def read(self, n=-1):
            return self.inner.read(min(n, 65537) if n and n > 0 else n)

        def close(self):
            self.inner.close()
            super().close()

    class Popen(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            if "s16le" in (a[0] if a else k.get("args", ())):
                self.stdout = Dribble(self.stdout)

    monkeypatch.setattr(subprocess, "Popen", Popen)
    heard = audio_events.listen(write_wav(tmp_path / "m.wav"))
    clapping = heard.times[heard.applause > 0.5]
    assert 99.5 <= clapping.min() and clapping.max() <= 104.5
