"""Word-level transcription with faster-whisper: large-v3 on GPU, speech windows heard in batches, hotwords,
hallucination guard, and a second take of the stretches where Whisper stopped writing punctuation."""

from __future__ import annotations

import gc
import hashlib
import os
import sys
import zlib
from contextlib import nullcontext
from difflib import SequenceMatcher
from functools import cache, partial
from pathlib import Path

from .i18n import CAPTION_LANGUAGES, DEFAULT, language_name
from .text import fold, spoken
from .tools import decode_audio, log, read_json, report, write_json

TRANSCRIBE_VERSION = "v3"
RATE = 16000
SENTENCE_MARKS = (".", "?", "!", "…", "。", "？", "！")
BARE_RUN = 30
REDO_PAD = 0.5
FIRST_TAKE = 0.85
DEFAULT_BATCH = 1
WINDOW = 29.0
NO_SPEECH = 0.6
LOW_LOGPROB = -1.0
REPEATS = 2.4
HALLUCINATION_GAP = 2.0
SPARSE = 1.5
SPARSE_WINDOW = 8.0


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


def _pipeline(model):
    from faster_whisper import BatchedInferencePipeline

    return BatchedInferencePipeline(model)


def batch_size() -> int:
    """Speech windows Whisper hears at once on the GPU (CLIPPY_WHISPER_BATCH); 1 hears them one after another."""
    try:
        return max(1, int(os.environ.get("CLIPPY_WHISPER_BATCH", DEFAULT_BATCH)))
    except ValueError:
        return DEFAULT_BATCH


def _batched(run, batch: int):
    """`run(batch)`, the batch halved while the GPU runs out of memory. Returns (result, the batch that worked); the
    result is None when not even a batch of 2 fits, and the caller hears the audio unbatched."""
    while batch > 1:
        try:
            return run(batch), batch
        except RuntimeError as exc:
            if "out of memory" not in str(exc).lower():
                raise
            batch //= 2
            gc.collect()
            log(f"[transcribe] GPU memory full, {f'batches of {batch}' if batch > 1 else 'one window at a time'}")
    return None, 1


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
        options = dict(language=language, word_timestamps=True, beam_size=5, best_of=5, vad_filter=True,
                       vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 400},
                       condition_on_previous_text=False, hallucination_silence_threshold=2.0, hotwords=vocab or None)
        out_segments, batch = _batched(partial(_first_take_batched, model, audio, options),
                                       batch_size() if used == "cuda" else 1)
        if out_segments is None:
            out_segments = _first_take(model, audio, options)
        _repunctuate(model, audio, out_segments, options, batch)
    except RuntimeError as exc:
        if used == "cuda" and ("cudnn" in str(exc).lower() or "cublas" in str(exc).lower()):
            log("[transcribe] CUDA library missing, retrying on CPU…")
            return _transcribe(cache, model_name, "cpu", language, vocab, src, isolated)
        raise

    result = {"language": language, "duration": len(audio) / RATE, "model": model_name, "segments": out_segments}
    write_json(cache, result)
    del model
    if used == "cuda":
        gc.collect()
    n_words = sum(len(s["words"]) for s in out_segments)
    log(f"[transcribe] done: {len(out_segments)} segments, {n_words} words, language: {language}")
    return result


def _segment(seg, offset: float = 0.0) -> dict | None:
    words = [
        {"start": round(w.start + offset, 3), "end": round(w.end + offset, 3), "word": w.word.strip(),
         "p": round(float(w.probability), 3)}
        for w in (seg.words or [])
        if w.word.strip()
    ]
    if not words:
        return None
    return {"start": round(seg.start + offset, 3), "end": round(seg.end + offset, 3), "text": seg.text.strip(),
            "words": words}


def _progress(heard: list[dict], duration: float) -> None:
    report(FIRST_TAKE * heard[-1]["end"] / max(1.0, duration or heard[-1]["end"]))
    if len(heard) % 50 == 0:
        log(f"[transcribe] {heard[-1]['end'] / 60:.1f} min done…")


def _take(model, audio, lo: float, hi: float, options: dict) -> list[dict]:
    """audio[lo:hi] heard on its own, unbatched, with the times in the whole audio."""
    segments, _ = model.transcribe(audio[int(lo * RATE):int(hi * RATE)], **options)
    return [s for s in (_segment(seg, lo) for seg in segments) if s]


def _first_take(model, audio, options: dict) -> list[dict]:
    heard: list[dict] = []
    segments, _ = model.transcribe(audio, **options)
    for seg in segments:
        if (item := _segment(seg)) is not None:
            heard.append(item)
            _progress(heard, len(audio) / RATE)
    return heard


def _batch_options(options: dict) -> dict:
    """The options the batched decoder takes; it has no prompt carry-over and no hallucination_silence_threshold."""
    unsupported = ("condition_on_previous_text", "hallucination_silence_threshold")
    opts = {k: v for k, v in options.items() if k not in unsupported}
    opts["vad_parameters"] = dict(options["vad_parameters"])
    return opts


def _first_take_batched(model, audio, options: dict, batch: int) -> list[dict]:
    """The speech windows heard `batch` at a time, each as one piece of text (with timestamp tokens the batched decoder
    drops what follows a window's last timestamp). It has no temperature fallback and no hallucination guard, so a
    window it got wrong is heard again unbatched, a silent one is dropped, and an improbable line between long
    silences is dropped as unbatched Whisper skips it."""
    segments, info = _pipeline(model).transcribe(audio, batch_size=batch, without_timestamps=True,
                                                 **_batch_options(options))
    windows: dict[int, tuple[list, list[dict]]] = {}
    heard: list[dict] = []
    for seg in segments:
        raw, items = windows.setdefault(seg.seek, ([], []))
        raw.append(seg)
        for item in lines(_segment(seg)):
            items.append(item)
            heard.append(item)
            _progress(heard, len(audio) / RATE)
    order = list(windows.values())
    bounds = [*windows, round(info.duration_after_vad * model.frames_per_second)]
    out: list[dict] = []
    retaken = silent = 0
    for n, (raw, items) in enumerate(order):
        verdict = window_verdict(raw, (bounds[n + 1] - bounds[n]) / model.frames_per_second)
        if verdict == "silent":
            silent += 1
            continue
        if verdict == "retake" and items:
            later = next((w[1][0] for w in order[n + 1:] if w[1]), None)
            items = _take(model, audio, out[-1]["end"] if out else 0.0,
                          later["start"] if later else len(audio) / RATE, options)
            retaken += 1
        out.extend(items)
    kept = drop_hallucinations(out)
    if retaken or silent or len(kept) < len(out):
        log(f"[transcribe] {retaken} windows heard again, {silent} silent windows and {len(out) - len(kept)} "
            f"improbable lines left out")
    return kept


def lines(segment: dict | None) -> list[dict]:
    """A batched window (up to 30 s of speech) cut where it is silent for more than HALLUCINATION_GAP seconds."""
    if segment is None:
        return []
    groups = [[segment["words"][0]]]
    for w in segment["words"][1:]:
        if w["start"] - groups[-1][-1]["end"] > HALLUCINATION_GAP:
            groups.append([])
        groups[-1].append(w)
    if len(groups) == 1:
        return [segment]
    return [{"start": g[0]["start"], "end": g[-1]["end"], "text": " ".join(w["word"] for w in g), "words": g}
            for g in groups]


def window_verdict(window: list, seconds: float) -> str:
    """'silent', 'retake' or 'ok' for one batched window of `seconds` of speech, by unbatched Whisper's own
    thresholds, and 'retake' when it heard under SPARSE words a second (the batched decoder now and then stops early
    and leaves the rest of the window unwritten)."""
    avg_logprob = window[0].avg_logprob
    if window[0].no_speech_prob > NO_SPEECH and avg_logprob < LOW_LOGPROB:
        return "silent"
    text = "".join(seg.text for seg in window).strip().encode("utf-8")
    if avg_logprob < LOW_LOGPROB or (text and len(text) / len(zlib.compress(text)) > REPEATS):
        return "retake"
    said = sum(1 for seg in window for w in (seg.words or []) if _key(w.word))
    if seconds >= SPARSE_WINDOW and said < SPARSE * seconds:
        return "retake"
    return "ok"


def _anomaly(word: dict) -> float:
    duration = word["end"] - word["start"]
    score = 1.0 if word["p"] < 0.15 else 0.0
    if duration < 0.133:
        score += (0.133 - duration) * 15
    if duration > 2.0:
        score += duration - 2.0
    return score


def _improbable(segment: dict | None) -> bool:
    words = [w for w in (segment or {}).get("words", []) if _key(w["word"])][:8]
    score = sum(map(_anomaly, words))
    return bool(words) and (score >= 3 or score + 0.01 >= len(words))


def drop_hallucinations(segments: list[dict]) -> list[dict]:
    """A line of improbable words with more than HALLUCINATION_GAP seconds of silence (or more such lines) on both
    sides is made up, as faster-whisper's hallucination_silence_threshold judges it in unbatched mode."""
    kept: list[dict] = []
    for n, seg in enumerate(segments):
        if _improbable(seg):
            later = segments[n + 1] if n + 1 < len(segments) else None
            gap = seg["start"] - (kept[-1]["end"] if kept else 0.0)
            before = seg["start"] < HALLUCINATION_GAP or gap > HALLUCINATION_GAP
            after = later is None or later["start"] - seg["end"] > HALLUCINATION_GAP or _improbable(later)
            if before and after:
                continue
        kept.append(seg)
    return kept


def _ends(word: str) -> bool:
    return word.rstrip("\"”»)'").endswith(SENTENCE_MARKS)


def bare_runs(words: list[dict]) -> list[tuple[int, int]]:
    """Stretches of BARE_RUN or more words with no sentence end (first and last index): Whisper now and then stops
    writing punctuation for minutes, which leaves a clip nowhere to end."""
    runs, start = [], 0
    for i, w in enumerate(words):
        if _ends(w["word"]):
            if i + 1 - start >= BARE_RUN:
                runs.append((start, i))
            start = i + 1
    if len(words) - start >= BARE_RUN:
        runs.append((start, len(words) - 1))
    return runs


def _key(word: str) -> str:
    return "".join(c for c in fold(word) if c.isalnum())


def carry_marks(old: list[dict], fresh: list[str]) -> int:
    """The punctuation and casing of a second take of the same words, put on the old words (their times stay). A
    take that heard a different number of words or no more sentence ends is not used. Returns the words changed."""
    if not old or not 0.8 <= len(fresh) / len(old) <= 1.25:
        return 0
    if sum(map(_ends, fresh)) <= sum(_ends(w["word"]) for w in old):
        return 0
    changed = 0
    matcher = SequenceMatcher(None, [_key(w["word"]) for w in old], [_key(w) for w in fresh], autojunk=False)
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            w, new = old[block.a + k], fresh[block.b + k]
            if _key(w["word"]) and new != w["word"]:
                w["word"] = new
                changed += 1
    return changed


def pieces(words: list[dict]) -> list[tuple[float, float]]:
    """A stretch cut between words into pieces of at most WINDOW seconds, so each fits one batched window whole."""
    lo = max(0.0, words[0]["start"] - REDO_PAD)
    out = []
    for a, b in zip(words, words[1:]):
        if b["end"] + REDO_PAD - lo > WINDOW:
            cut = round((a["end"] + b["start"]) / 2, 3)
            out.append((lo, cut))
            lo = cut
    out.append((lo, words[-1]["end"] + REDO_PAD))
    return out


def _retakes_batched(model, audio, words: list[dict], runs: list[tuple[int, int]], options: dict,
                     batch: int) -> list[list[str]]:
    cuts = [(r, lo, hi) for r, (i, j) in enumerate(runs) for lo, hi in pieces(words[i:j + 1])]
    owner = {int(int(lo * RATE) / RATE * model.frames_per_second): (r, k) for k, (r, lo, _) in enumerate(cuts)}
    segments, _ = _pipeline(model).transcribe(audio, batch_size=batch, without_timestamps=True,
                                              clip_timestamps=[{"start": lo, "end": hi} for _, lo, hi in cuts],
                                              **_batch_options(options))
    takes: list[list[str]] = [[] for _ in runs]
    for seg in segments:
        r, k = owner[seg.seek]
        takes[r].extend(w.word.strip() for w in (seg.words or []) if w.word.strip())
        report(FIRST_TAKE + (1 - FIRST_TAKE) * (k + 1) / len(cuts))
    return takes


def _repunctuate(model, audio, segments: list[dict], options: dict, batch: int = 1) -> None:
    """Each long unpunctuated stretch heard again on its own, where Whisper writes its sentences out."""
    words = [w for seg in segments for w in seg["words"]]
    runs = bare_runs(words)
    if not runs:
        report(1.0)
        return
    log(f"[transcribe] {len(runs)} stretches without punctuation, listening to them again…")
    takes, _ = _batched(partial(_retakes_batched, model, audio, words, runs, options), batch)
    if takes is None:
        takes = []
        for n, (i, j) in enumerate(runs):
            lo, hi = max(0.0, words[i]["start"] - REDO_PAD), words[j]["end"] + REDO_PAD
            takes.append([w["word"] for s in _take(model, audio, lo, hi, options) for w in s["words"]])
            report(FIRST_TAKE + (1 - FIRST_TAKE) * (n + 1) / len(runs))
    fixed = sum(carry_marks(words[i:j + 1], fresh) > 0 for (i, j), fresh in zip(runs, takes))
    for seg in segments:
        if " " in seg["text"]:
            seg["text"] = " ".join(w["word"] for w in seg["words"])
    log(f"[transcribe] punctuation back in {fixed} of {len(runs)} stretches")


def all_words(transcript: dict) -> list[dict]:
    """Every word said, in order; emoji Whisper wrote (from a title in its hints) are not words."""
    return spoken([w for seg in transcript["segments"] for w in seg["words"]])
