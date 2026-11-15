"""Forced alignment (torchaudio MMS_FA) that puts every Whisper word on its acoustic frames."""

from __future__ import annotations

import re
import unicodedata
from contextlib import nullcontext
from pathlib import Path

import numpy as np

from .audio import envelope, voice_threshold
from .tools import decode_audio, log, read_json, report, write_json

SR = 16000


def available() -> bool:
    """torchaudio with the MMS_FA aligner (removed after torchaudio 2.8, hence the pin)."""
    try:
        import torchaudio

        return hasattr(torchaudio.pipelines, "MMS_FA")
    except Exception:
        return False


def romanize(word: str) -> str:
    s = unicodedata.normalize("NFKD", word.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z']", "", s)
    return s or "*"


def refine_onsets(transcript: dict, audio_path: Path, min_gap: float = 0.2) -> dict:
    """Snap word starts after a pause (and ends before one) to the acoustic onset."""
    env = envelope(audio_path)
    n = len(env)
    if n < 10:
        return transcript
    voiced = env > voice_threshold(env)

    words = [w for s in transcript["segments"] for w in s["words"]]
    fixed = 0
    for k, w in enumerate(words):
        prev = words[k - 1] if k else None
        nxt = words[k + 1] if k + 1 < len(words) else None
        if prev is None or w["start"] - prev["end"] >= min_gap:
            a = int(((prev["end"] + 0.03) if prev else max(0.0, w["start"] - 0.5)) * 100)
            b = min(n, int((w["start"] + 0.45) * 100))
            if b - a > 3:
                win = voiced[a:b]
                for i in range(1, len(win) - 2):
                    if not win[i - 1] and win[i] and win[i + 1] and win[i + 2]:
                        onset = (a + i) / 100
                        if abs(onset - w["start"]) > 0.03 and onset < w["end"] - 0.05:
                            w["start"] = round(onset, 3)
                            fixed += 1
                        break
        if nxt is None or nxt["start"] - w["end"] >= min_gap:
            a = max(0, int((w["end"] - 0.45) * 100))
            b = min(n, int(((nxt["start"] - 0.03) if nxt else w["end"] + 0.5) * 100))
            if b - a > 3:
                win = voiced[a:b]
                for i in range(len(win) - 2, 1, -1):
                    if win[i - 2] and win[i - 1] and win[i] and not win[i + 1]:
                        offset = (a + i + 1) / 100 + 0.03
                        if offset > w["start"] + 0.05 and abs(offset - w["end"]) > 0.03:
                            w["end"] = round(offset, 3)
                        break
    for s in transcript["segments"]:
        if s["words"]:
            s["start"], s["end"] = s["words"][0]["start"], s["words"][-1]["end"]
    log(f"[align] {fixed} word starts snapped to the acoustic onset")
    return transcript


def align(transcript: dict, audio_path: Path, cache: Path, gpu=nullcontext) -> dict:
    if cache.exists():
        log("[align] cache")
        return read_json(cache)
    with gpu():
        return _align(transcript, audio_path, cache)


def _align(transcript: dict, audio_path: Path, cache: Path) -> dict:
    import warnings

    import torch
    import torchaudio

    warnings.filterwarnings("ignore", message=".*forced_align.*")
    warnings.filterwarnings("ignore", category=UserWarning, module="torchaudio")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    log(f"[align] word-level forced alignment (MMS_FA, {device})…")
    bundle = torchaudio.pipelines.MMS_FA
    model = bundle.get_model(with_star=True).to(device).eval()
    tokenizer = bundle.get_tokenizer()
    aligner = bundle.get_aligner()

    audio = decode_audio(audio_path, sr=SR)
    moved = kept = 0
    segments = []
    total = max(1, len(transcript["segments"]))
    for i, seg in enumerate(transcript["segments"]):
        if i % 20 == 0:
            report(i / total)
        words = seg["words"]
        if not words:
            segments.append(seg)
            continue
        t0 = max(0.0, words[0]["start"] - 0.4)
        t1 = min(len(audio) / SR, words[-1]["end"] + 0.4)
        chunk = audio[int(t0 * SR): int(t1 * SR)]
        new_words = [dict(w) for w in words]
        try:
            tokens = tokenizer([romanize(w["word"]) for w in words])
            wave = torch.from_numpy(np.ascontiguousarray(chunk))[None].to(device)
            with torch.inference_mode():
                emission, _ = model(wave)
                spans = aligner(emission[0], tokens)
            ratio = chunk.shape[0] / emission.shape[1] / SR
            for w, sp, orig in zip(new_words, spans, words):
                if not sp:
                    kept += 1
                    continue
                s = t0 + sp[0].start * ratio
                e = t0 + sp[-1].end * ratio
                score = float(np.mean([x.score for x in sp]))
                if romanize(orig["word"]) == "*" or score < 0.1 or abs(s - orig["start"]) > 0.8 or e <= s:
                    kept += 1
                    continue
                w["start"], w["end"] = round(s, 3), round(e, 3)
                moved += 1
        except Exception as exc:
            kept += len(words)
            log(f"[align] segment skipped ({type(exc).__name__})")
        for a, b in zip(new_words, new_words[1:]):
            if b["start"] < a["start"]:
                b["start"] = a["start"]
            if a["end"] > b["start"]:
                a["end"] = max(a["start"] + 0.02, b["start"])
        segments.append({**seg, "words": new_words, "start": new_words[0]["start"], "end": new_words[-1]["end"]})

    result = {**transcript, "segments": segments, "aligned": True}
    write_json(cache, result)
    log(f"[align] done: {moved} words re-timed, {kept} kept Whisper timing")
    del model
    if device == "cuda":
        torch.cuda.empty_cache()
    return result
