from types import SimpleNamespace

import numpy as np
import pytest

from clippy import transcribe

OOM = "CUDA failed with error out of memory"


def _word(word, start, end, p=0.9):
    return SimpleNamespace(word=f" {word}", start=start, end=end, probability=p)


def _seg(text, start, seek=0, avg_logprob=-0.2, no_speech_prob=0.01, step=0.4, p=0.9):
    words = [_word(w, start + k * step, start + k * step + step * 0.8, p) for k, w in enumerate(text.split())]
    return SimpleNamespace(seek=seek, start=start, end=words[-1].end, text=f" {text}", words=words,
                           avg_logprob=avg_logprob, no_speech_prob=no_speech_prob)


class Model:
    frames_per_second = 100

    def __init__(self, device="cuda", segments=()):
        self.device = device
        self.segments = list(segments)
        self.calls = []

    def detect_language(self, audio, **kwargs):
        return "hu", 0.9, [("hu", 0.9)]

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        return iter(self.segments), SimpleNamespace(language="hu", duration=len(audio) / 16000)


class Pipeline:
    def __init__(self, segments, fits=0, speech=2.0):
        self.segments = segments
        self.fits = fits
        self.speech = speech
        self.batches = []

    def transcribe(self, audio, batch_size, **kwargs):
        self.batches.append(batch_size)
        if batch_size > self.fits:
            raise RuntimeError(OOM)
        segments = self.segments(kwargs) if callable(self.segments) else self.segments
        return iter(segments), SimpleNamespace(duration_after_vad=self.speech)


def _run(tmp_path, monkeypatch, model, pipeline, seconds=10):
    monkeypatch.setattr(transcribe, "cuda_available", lambda: model.device == "cuda")
    monkeypatch.setattr(transcribe, "_load_model", lambda name, device: (model, model.device))
    monkeypatch.setattr(transcribe, "_pipeline", lambda m: pipeline)
    monkeypatch.setattr(transcribe, "decode_audio", lambda path: np.zeros(16000 * seconds, np.float32))
    video = tmp_path / "video.mkv"
    video.write_bytes(b"x")
    result = transcribe.transcribe(video, tmp_path)
    return [w["word"] for s in result["segments"] for w in s["words"]]


def test_the_batch_shrinks_on_gpu_out_of_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPPY_WHISPER_BATCH", "8")
    pipeline = Pipeline([_seg("Szia, ez egy teszt.", 1.0)], fits=2)
    model = Model()
    assert _run(tmp_path, monkeypatch, model, pipeline) == ["Szia,", "ez", "egy", "teszt."]
    assert pipeline.batches == [8, 4, 2]
    assert model.calls == []


def test_unbatched_whisper_takes_over_when_no_batch_fits(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPPY_WHISPER_BATCH", "4")
    pipeline = Pipeline([], fits=0)
    model = Model(segments=[_seg("Megvan.", 0.5)])
    assert _run(tmp_path, monkeypatch, model, pipeline) == ["Megvan."]
    assert pipeline.batches == [4, 2]
    assert model.calls[0]["hallucination_silence_threshold"] == 2.0
    assert model.calls[0]["vad_filter"] and model.calls[0]["beam_size"] == 5


def test_other_gpu_errors_are_not_taken_for_a_full_memory(monkeypatch):
    def run(batch):
        raise RuntimeError("CUDA failed with error an illegal memory access was encountered")

    with pytest.raises(RuntimeError, match="illegal"):
        transcribe._batched(run, 8)


def test_cpu_and_a_batch_of_one_hear_the_audio_unbatched(tmp_path, monkeypatch):
    (tmp_path / "cpu").mkdir()
    assert _run(tmp_path / "cpu", monkeypatch, Model(device="cpu", segments=[_seg("Jó.", 0.5)]), None) == ["Jó."]
    monkeypatch.setenv("CLIPPY_WHISPER_BATCH", "1")
    (tmp_path / "one").mkdir()
    pipeline = Pipeline([], fits=99)
    assert _run(tmp_path / "one", monkeypatch, Model(segments=[_seg("Jó.", 0.5)]), pipeline) == ["Jó."]
    assert pipeline.batches == []


@pytest.mark.parametrize("value, size", [("16", 16), ("0", 1), ("sok", transcribe.DEFAULT_BATCH),
                                         ("", transcribe.DEFAULT_BATCH)])
def test_the_batch_size_setting(monkeypatch, value, size):
    monkeypatch.setenv("CLIPPY_WHISPER_BATCH", value)
    assert transcribe.batch_size() == size


def test_a_window_is_judged_by_whispers_own_thresholds():
    assert transcribe.window_verdict([_seg("hmm", 0, avg_logprob=-1.2, no_speech_prob=0.9)], 2.0) == "silent"
    assert transcribe.window_verdict([_seg("talán", 0, avg_logprob=-1.2)], 2.0) == "retake"
    assert transcribe.window_verdict([_seg(" ".join(["igen"] * 60), 0)], 20.0) == "retake"
    assert transcribe.window_verdict([_seg("Ez egy rendes mondat.", 0)], 2.0) == "ok"


def test_a_window_that_stopped_writing_early_is_heard_again():
    said = _seg(" ".join(f"szó{k}" for k in range(20)), 0)
    assert transcribe.window_verdict([said], 25.0) == "retake"
    assert transcribe.window_verdict([said], 10.0) == "ok"
    assert transcribe.window_verdict([_seg("Csak ennyi.", 0)], 5.0) == "ok"


def test_a_wrong_window_is_heard_again_unbatched_and_a_silent_one_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPPY_WHISPER_BATCH", "8")
    batched = [_seg("Első mondat.", 0.5, seek=0), _seg("igen igen igen", 3.0, seek=300, avg_logprob=-1.5),
               _seg("Harmadik.", 6.0, seek=600), _seg("zaj", 8.0, seek=800, avg_logprob=-2, no_speech_prob=0.95)]
    model = Model(segments=[_seg("Második mondat.", 0.4)])
    words = _run(tmp_path, monkeypatch, model, Pipeline(batched, fits=8, speech=9.0))
    assert words == ["Első", "mondat.", "Második", "mondat.", "Harmadik."]
    assert len(model.calls) == 1 and model.calls[0]["hallucination_silence_threshold"] == 2.0
    retake = [w for s in transcribe.read_json(next(tmp_path.glob("*.transcript.json")))["segments"]
              for w in s["words"] if w["word"] == "Második"][0]
    assert retake["start"] > 1.0


def _line(text, start, p=0.9):
    return transcribe._segment(_seg(text, start, p=p))


def test_an_improbable_line_between_long_silences_is_dropped():
    made_up = _line("Köszönöm a figyelmet", 20.0, p=0.05)
    speech = [_line("Ez itt beszéd.", 1.0), made_up, _line("Újra beszéd.", 40.0)]
    assert transcribe.drop_hallucinations(speech) == [speech[0], speech[2]]
    window = _line("Ez itt beszéd.", 1.0)
    window["words"] += made_up["words"] + _line("Újra beszéd.", 40.0)["words"]
    parts = transcribe.lines(window)
    assert [p["text"] for p in parts] == ["Ez itt beszéd.", "Köszönöm a figyelmet", "Újra beszéd."]
    assert transcribe.drop_hallucinations(parts) == [parts[0], parts[2]]
    close = [_line("Ez itt beszéd.", 1.0), _line("halk szavak", 2.5, p=0.05), _line("Újra beszéd.", 4.0)]
    assert transcribe.drop_hallucinations(close) == close


def test_a_long_stretch_is_cut_between_words_into_whole_windows():
    words = [{"word": "szó", "start": k * 0.5, "end": k * 0.5 + 0.4} for k in range(200)]
    cuts = transcribe.pieces(words)
    assert len(cuts) == 4
    assert all(hi - lo <= transcribe.WINDOW for lo, hi in cuts)
    assert all(a[1] == b[0] for a, b in zip(cuts, cuts[1:]))
    assert cuts[0][0] == 0.0 and cuts[-1][1] == pytest.approx(words[-1]["end"] + transcribe.REDO_PAD)


def test_stretches_without_punctuation_are_heard_again_in_one_batch(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPPY_WHISPER_BATCH", "8")
    bare = " ".join(f"szo{k}" for k in range(transcribe.BARE_RUN + 5))
    batched = [_seg(bare, 1.0, step=0.5)]

    def retakes(kwargs):
        assert kwargs["hotwords"] is None and kwargs["word_timestamps"]
        out = []
        for clip in kwargs["clip_timestamps"]:
            seek = int(int(clip["start"] * 16000) / 16000 * 100)
            words = [f"szo{k}" for k in range(transcribe.BARE_RUN + 5)
                     if clip["start"] <= 1.0 + k * 0.5 < clip["end"]]
            out.append(_seg(" ".join(w if (n + 1) % 10 else w + "." for n, w in enumerate(words)), clip["start"],
                            seek=seek))
        return out

    calls = []

    def pipeline_for(m):
        calls.append(1)
        return Pipeline(batched if len(calls) == 1 else retakes, fits=8)

    model = Model()
    monkeypatch.setattr(transcribe, "cuda_available", lambda: True)
    monkeypatch.setattr(transcribe, "_load_model", lambda name, device: (model, "cuda"))
    monkeypatch.setattr(transcribe, "_pipeline", pipeline_for)
    monkeypatch.setattr(transcribe, "decode_audio", lambda path: np.zeros(16000 * 30, np.float32))
    video = tmp_path / "video.mkv"
    video.write_bytes(b"x")
    result = transcribe.transcribe(video, tmp_path)
    words = [w["word"] for s in result["segments"] for w in s["words"]]
    assert len(calls) == 2 and model.calls == []
    assert sum(w.endswith(".") for w in words) >= 3
