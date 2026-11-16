"""Voice input for the chat: a short recording becomes text with a small Whisper model on the CPU, so it never waits
for (or slows down) a job that uses the GPU."""

from __future__ import annotations

import threading
from pathlib import Path

from .tools import ClippyError, decode_audio

MODEL = "small"
MAX_SECONDS = 120
_model = None
_lock = threading.Lock()


def _whisper():
    global _model
    with _lock:
        if _model is None:
            from faster_whisper import WhisperModel

            _model = WhisperModel(MODEL, device="cpu", compute_type="int8")
        return _model


def dictate(audio: Path) -> str:
    """The words of a recording, in whatever language it was spoken."""
    samples = decode_audio(audio)
    if len(samples) > (MAX_SECONDS + 5) * 16000:
        raise ClippyError(key="error.dictation_long", max=MAX_SECONDS)
    try:
        segments, _ = _whisper().transcribe(samples, language=None,
                                            vad_filter=True, beam_size=5, condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segments).strip()
    except Exception as exc:
        raise ClippyError(key="error.dictation_failed", error=f"{type(exc).__name__}: {str(exc)[:120]}") from exc
