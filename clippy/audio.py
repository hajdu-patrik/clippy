"""Sound for short-form: click-free joins, voice EQ and compression, two-pass -14 LUFS loudness."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import numpy as np

from .tools import decode_audio, ffmpeg_path, log

TARGET_I = -14.0
TARGET_TP = -1.0
TARGET_LRA = 11.0
HOP = 0.01


def envelope_db(samples: np.ndarray, sr: int = 16000, hop: float = HOP) -> np.ndarray:
    """RMS level in dBFS every `hop` seconds."""
    n = int(sr * hop)
    frames = len(samples) // n
    if frames == 0:
        return np.full(1, -90.0)
    rms = np.sqrt(np.mean(samples[: frames * n].reshape(frames, n) ** 2, axis=1) + 1e-12)
    return 20 * np.log10(rms + 1e-9)


def voice_threshold(env: np.ndarray) -> float:
    """dB level between the noise floor and speech: above it a 10 ms block counts as voiced."""
    floor, speech = float(np.percentile(env, 10)), float(np.percentile(env, 90))
    return floor + 0.35 * max(6.0, speech - floor)


def envelope(path: Path, start: float | None = None, duration: float | None = None) -> np.ndarray:
    """Envelope of a file (or a span of it), used for cut points and onsets."""
    return envelope_db(decode_audio(path, start=start, duration=duration))


def segments_graph(intervals: list[tuple[float, float]], src: str = "0:a") -> str:
    """atrim each kept interval with micro-fades, concat them -> [cat]."""
    parts = []
    n = len(intervals)
    for k, (a, b) in enumerate(intervals):
        length = b - a
        fin = 0.015 if k == 0 else 0.006
        fout = 0.08 if k == n - 1 else 0.006
        fout_st = max(0.0, length - fout)
        parts.append(
            f"[{src}]atrim=start={a:.4f}:end={b:.4f},asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d={fin},afade=t=out:st={fout_st:.4f}:d={fout}[s{k}]"
        )
    labels = "".join(f"[s{k}]" for k in range(n))
    parts.append(f"{labels}concat=n={n}:v=0:a=1[cat]")
    return ";".join(parts)


def processing_chain(mode: str) -> str:
    if mode == "voice":
        return ("highpass=f=75:poles=2,"
                "equalizer=f=250:t=q:w=1.2:g=-1.5,"
                "equalizer=f=3500:t=q:w=1.0:g=1.5,"
                "acompressor=threshold=0.09:ratio=2.5:attack=12:release=180:knee=4:makeup=1.6")
    return "highpass=f=30"


def measure(video: Path, start: float, span: float, intervals, mode: str) -> dict | None:
    """Pass 1 of loudnorm: measure the processed clip."""
    graph = (segments_graph(intervals, "0:a") + f";[cat]{processing_chain(mode)},"
             f"loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}:print_format=json[a]")
    cmd = [ffmpeg_path(), "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-t", f"{span:.3f}", "-i", str(video),
           "-filter_complex", graph, "-map", "[a]", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return parse_loudnorm(proc.stderr if proc.returncode == 0 else "")


def parse_loudnorm(stderr: str) -> dict | None:
    """The JSON that `loudnorm=print_format=json` prints; None when missing or the signal is (near) silent."""
    m = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr, flags=re.S)
    if not m:
        log("[audio] loudness measurement failed, single-pass normalisation")
        return None
    data = json.loads(m[-1])
    try:
        if float(data["input_i"]) < -70:
            return None
    except (KeyError, ValueError):
        return None
    return data


def loudnorm_options(measured: dict | None) -> dict:
    """loudnorm settings: a linear second pass when a measurement exists, dynamic single pass otherwise."""
    options = {"I": TARGET_I, "TP": TARGET_TP, "LRA": TARGET_LRA}
    if measured:
        options.update(measured_I=measured["input_i"], measured_TP=measured["input_tp"],
                       measured_LRA=measured["input_lra"], measured_thresh=measured["input_thresh"],
                       offset=measured["target_offset"], linear="true")
    return options


def loudnorm_filter(measured: dict | None) -> str:
    return ":".join(f"{k}={v}" for k, v in loudnorm_options(measured).items())


def final_graph(intervals, mode: str, measured: dict | None, src: str = "1:a") -> str:
    ln = f"loudnorm={loudnorm_filter(measured)}"
    return segments_graph(intervals, src) + f";[cat]{processing_chain(mode)},{ln},aresample=48000[a]"
