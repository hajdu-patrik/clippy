"""Who speaks when, from the voice alone: every sentence gets a voice print (the average spectrum shape, as MFCCs)
and the prints are grouped into speakers. Light and local, no model to download; it only labels the speakers when
the voices clearly differ (a podcast host and a guest), otherwise it says nothing."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

from .tools import ffmpeg_path, log, write_json

RATE = 16000
FFT = 512
HOP = 256
BANDS = 26
COEFFS = 13
MIN_SENTENCE = 1.0
MAX_SPEAKERS = 4
MIN_SILHOUETTE = 0.18
MIN_SHARE = 0.08
SAMPLE = 700
LABELS = "ABCD"
VOICED_DB = 30.0


def mel_filters(rate: int = RATE, fft: int = FFT, bands: int = BANDS) -> np.ndarray:
    def mel(f):
        return 2595 * np.log10(1 + f / 700)

    def hz(m):
        return 700 * (10 ** (m / 2595) - 1)

    points = hz(np.linspace(mel(80), mel(rate / 2 * 0.9), bands + 2))
    bins = np.floor((fft + 1) * points / rate).astype(int)
    out = np.zeros((bands, fft // 2 + 1))
    for b in range(bands):
        lo, mid, hi = bins[b], bins[b + 1], max(bins[b + 2], bins[b + 1] + 1)
        out[b, lo:mid] = np.linspace(0, 1, max(1, mid - lo), endpoint=False)
        out[b, mid:hi] = np.linspace(1, 0, max(1, hi - mid), endpoint=False)
    return out


def dct_matrix(n: int = BANDS, k: int = COEFFS) -> np.ndarray:
    i = np.arange(n)
    return np.array([np.cos(np.pi * q * (2 * i + 1) / (2 * n)) for q in range(1, k + 1)])


def frame_prints(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """(times, MFCCs) of the voiced frames of a whole file, decoded as a stream."""
    window, filters, dct = np.hanning(FFT), mel_filters(), dct_matrix()
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(RATE),
           "-f", "s16le", "-"]
    times, prints, offset, rest, odd = [], [], 0, np.zeros(0, np.float32), b""
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        while chunk := proc.stdout.read(RATE * 2 * 30):
            data = odd + chunk
            odd = data[len(data) // 2 * 2:]
            audio = np.concatenate([rest, np.frombuffer(data[:len(data) // 2 * 2], np.int16) / 32768.0])
            count = max(0, (len(audio) - FFT) // HOP + 1)
            if count:
                idx = np.arange(FFT)[None, :] + HOP * np.arange(count)[:, None]
                frames = audio[idx] * window
                power = np.abs(np.fft.rfft(frames, axis=1)) ** 2
                energy = 10 * np.log10(power.sum(axis=1) + 1e-10)
                voiced = energy > np.percentile(energy, 95) - VOICED_DB
                logmel = np.log(power[voiced] @ filters.T + 1e-10)
                prints.append(logmel @ dct.T)
                times.append((offset + HOP * np.arange(count)[voiced] + FFT / 2) / RATE)
                offset += HOP * count
                rest = audio[HOP * count:]
            else:
                rest = audio
    if not prints:
        return np.zeros(0), np.zeros((0, COEFFS))
    return np.concatenate(times), np.concatenate(prints)


def sentence_prints(times: np.ndarray, prints: np.ndarray, sentences: list[dict]) -> tuple[list[int], np.ndarray]:
    """The mean and spread of the voice print over each long enough sentence."""
    keep, rows = [], []
    for i, s in enumerate(sentences):
        if s["end"] - s["start"] < MIN_SENTENCE:
            continue
        a, b = np.searchsorted(times, s["start"]), np.searchsorted(times, s["end"])
        if b - a < 20:
            continue
        part = prints[a:b]
        keep.append(i)
        rows.append(np.concatenate([part.mean(axis=0), part.std(axis=0)]))
    return keep, np.array(rows)


def kmeans(x: np.ndarray, k: int, rounds: int = 30, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    centres = [x[rng.integers(len(x))]]
    for _ in range(1, k):
        d = np.min([((x - c) ** 2).sum(axis=1) for c in centres], axis=0)
        centres.append(x[rng.choice(len(x), p=d / d.sum()) if d.sum() > 0 else rng.integers(len(x))])
    centres = np.array(centres)
    labels = np.zeros(len(x), int)
    for _ in range(rounds):
        labels = np.argmin(((x[:, None, :] - centres[None]) ** 2).sum(axis=2), axis=1)
        moved = np.array([x[labels == j].mean(axis=0) if np.any(labels == j) else centres[j] for j in range(k)])
        if np.allclose(moved, centres):
            break
        centres = moved
    return labels


def silhouette(x: np.ndarray, labels: np.ndarray) -> float:
    d = np.sqrt(((x[:, None, :] - x[None]) ** 2).sum(axis=2))
    scores = []
    for i in range(len(x)):
        own = labels == labels[i]
        if own.sum() < 2:
            scores.append(0.0)
            continue
        a = d[i, own].sum() / (own.sum() - 1)
        b = min(d[i, labels == j].mean() for j in set(labels.tolist()) if j != labels[i])
        scores.append((b - a) / max(a, b, 1e-9))
    return float(np.mean(scores))


def label(features: np.ndarray) -> list[int] | None:
    """Speaker numbers per row, or None when the voices do not clearly form two or more groups."""
    if len(features) < 12:
        return None
    x = (features - features.mean(axis=0)) / (features.std(axis=0) + 1e-6)
    sample = x if len(x) <= SAMPLE else x[np.linspace(0, len(x) - 1, SAMPLE).astype(int)]
    best, best_score = None, MIN_SILHOUETTE
    for k in range(2, MAX_SPEAKERS + 1):
        labels = kmeans(sample, k)
        shares = np.bincount(labels, minlength=k) / len(labels)
        if shares.min() < MIN_SHARE:
            continue
        score = silhouette(sample, labels)
        if score > best_score:
            best, best_score = k, score
    if best is None:
        return None
    labels = kmeans(x, best)
    order = {old: new for new, old in enumerate(dict.fromkeys(labels.tolist()))}
    return [order[v] for v in labels.tolist()]


def speaker_turns(path: Path, sentences: list[dict]) -> list[str]:
    """A speaker letter per sentence ("" for short ones between turns); all empty for one voice."""
    times, prints = frame_prints(path)
    keep, features = sentence_prints(times, prints, sentences)
    found = label(features) if len(features) else None
    out = [""] * len(sentences)
    if found is None:
        return out
    for i, n in zip(keep, found):
        out[i] = LABELS[n]
    last = ""
    for i in range(len(out)):
        out[i] = out[i] or last
        last = out[i]
    return out


def cached(path: Path, make) -> list[str]:
    try:
        return list(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        pass
    turns = make()
    write_json(path, turns)
    voices = sorted(set(t for t in turns if t))
    log(f"[hook] voices: {len(voices) or 1} speaker(s)" + (f" ({', '.join(voices)})" if voices else ""))
    return turns
