"""Word-level transcription with faster-whisper: large-v3 on GPU, hotwords, hallucination guard."""

from __future__ import annotations

import gc
import hashlib
import os
import sys
from contextlib import nullcontext
from functools import cache
from pathlib import Path

from .i18n import CAPTION_LANGUAGES, DEFAULT, language_name
from .tools import decode_audio, log, read_json, report, write_json

TRANSCRIBE_VERSION = "v2"


@cache
def _add_cuda_dll_dirs() -> None:
    """ctranslate2 on Windows loads cuBLAS / cuDNN from the nvidia-* wheels; added once, so PATH never grows."""
    if sys.platform != "win32":
        return
    try:
        import nvidia
    except ImportError:
        return
    for base in getattr(nvidia, "__path__", []):
        for sub in ("cublas", "cudnn", "cuda_runtime"):
            bin_dir = Path(base) / sub / "bin"
            if not bin_dir.is_dir():
                continue
            os.add_dll_directory(str(bin_dir))
            path = os.environ.get("PATH", "")
            if str(bin_dir) not in path.split(os.pathsep):
                os.environ["PATH"] = str(bin_dir) + os.pathsep + path


def cuda_available() -> bool:
    _add_cuda_dll_dirs()
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def resolve_model(model_name: str, device: str) -> tuple[str, str]:
    """'auto' model -> large-v3 on GPU, large-v3-turbo on CPU. Returns (model, device)."""
    use_cuda = device == "cuda" or (device == "auto" and cuda_available())
    dev = "cuda" if use_cuda else "cpu"
    if model_name == "auto":
        model_name = "large-v3" if use_cuda else "large-v3-turbo"
    return model_name, dev


def _load_model(model_name: str, device: str):
    from faster_whisper import WhisperModel

    if device == "cuda":
        _add_cuda_dll_dirs()
        try:
            return WhisperModel(model_name, device="cuda", compute_type="float16"), "cuda"
        except Exception as exc:
            log(f"[transcribe] GPU failed ({type(exc).__name__}: {exc}), running on CPU, slower.")
    return WhisperModel(model_name, device="cpu", compute_type="int8"), "cpu"


SURE = 0.7


def detect_language(model, audio) -> str:
    """The video's own language from Whisper's scores over several speech windows. An unsure guess for a language
    other than Hungarian or English gives way to the likelier of those two (noise and music fool the detector)."""
    try:
        _, _, scores = model.detect_language(audio, vad_filter=True, language_detection_segments=6)
        probs = dict(scores)
    except Exception as exc:
        log(f"[transcribe] language detection failed ({type(exc).__name__}), assuming English")
        return DEFAULT
    best = max(probs, key=probs.get) if probs else DEFAULT
    if best not in CAPTION_LANGUAGES and probs.get(best, 0.0) < SURE:
        best = max(CAPTION_LANGUAGES, key=lambda code: probs.get(code, 0.0))
    log(f"[transcribe] language: {language_name(best)} ({probs.get(best, 0):.2f})")
    return best


def transcribe(video: Path, work_dir: Path, model_name: str = "auto",
               language: str | None = None, device: str = "auto", vocab: str = "",
               audio_source: Path | None = None, gpu=nullcontext, sound: Path | None = None) -> dict:
    """Cached word-level transcript; `vocab` names are hotwords for every window."""
    model_name, dev = resolve_model(model_name, device)
    src = audio_source or video
    tag = hashlib.sha1(f"{TRANSCRIBE_VERSION}|{vocab}|{src.name}".encode("utf-8")).hexdigest()[:8]
    cache = work_dir / f"{video.stem}.{model_name}.{language or 'auto'}.{tag}.transcript.json"
    if cache.exists():
        log(f"[transcribe] cache: {cache.name}")
        return read_json(cache)
    with gpu():
        return _transcribe(cache, model_name, dev, language, vocab, audio_source or sound or src, bool(audio_source))


def _transcribe(cache: Path, model_name: str, dev: str, language: str | None, vocab: str, src: Path,
                isolated: bool) -> dict:
    model, used = _load_model(model_name, dev)
    log(f"[transcribe] {model_name}, {used}{', on the isolated voice track' if isolated else ''}, starting…")
    audio = decode_audio(src)
    if language is None:
        language = detect_language(model, audio)
    try:
        segments, info = model.transcribe(
            audio,
            language=language,
            word_timestamps=True,
            beam_size=5,
            best_of=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 400},
            condition_on_previous_text=False,
            hallucination_silence_threshold=2.0,
            hotwords=vocab or None,
        )
        out_segments = []
        for seg in segments:
            words = [
                {"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word.strip(),
                 "p": round(float(w.probability), 3)}
                for w in (seg.words or [])
                if w.word.strip()
            ]
            if not words:
                continue
            out_segments.append(
                {"start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text.strip(), "words": words}
            )
            report(seg.end / max(1.0, float(info.duration or 0) or seg.end))
            if len(out_segments) % 50 == 0:
                log(f"[transcribe] {seg.end / 60:.1f} min done…")
    except RuntimeError as exc:
        if used == "cuda" and ("cudnn" in str(exc).lower() or "cublas" in str(exc).lower()):
            log("[transcribe] CUDA library missing, retrying on CPU…")
            return _transcribe(cache, model_name, "cpu", language, vocab, src, isolated)
        raise

    result = {"language": info.language, "duration": info.duration, "model": model_name, "segments": out_segments}
    write_json(cache, result)
    del model
    if used == "cuda":
        gc.collect()
    n_words = sum(len(s["words"]) for s in out_segments)
    log(f"[transcribe] done: {len(out_segments)} segments, {n_words} words, language: {info.language}")
    return result


def all_words(transcript: dict) -> list[dict]:
    return [w for seg in transcript["segments"] for w in seg["words"]]
