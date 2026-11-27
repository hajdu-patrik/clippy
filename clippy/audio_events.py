"""Laughter, applause and cheering heard in the sound itself, by YAMNet (Google's AudioSet tagger, MobileNet, 3.7 M
weights) on the CPU through onnxruntime, so it never waits for the GPU. Optional: without the model or onnxruntime
the reactions keep the laughing words of the transcript only."""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .storage import MODELS
from .tools import atomic_path, ffmpeg_path, log, probe_media, report

YAMNET_URL = ("https://huggingface.co/zeropointnine/yamnet-onnx/resolve/"
              "ac2ca3bd45d12ec1f19f1144205ea529b4e9dedf/yamnet.onnx")
YAMNET_PATH = MODELS / "yamnet.onnx"
YAMNET_SHA256 = "1510041dce24a2e9e84ec546807ac408ae496da6d1ed41bc3ccba649623f8e19"
RATE = 16000
FRAME_HOP = 0.48
CHUNK_FRAMES = 250
THREADS = min(4, os.cpu_count() or 4)
CLASSES = {"laugh": (13, 15, 16, 17, 18), "applause": (58, 62), "cheer": (61,)}
_session = None
_ready: bool | None = None


@dataclass
class Heard:
    """Per YAMNet frame (every 0.48 s, centred on `times`): how sure the model is of each event, 0..1."""
    times: np.ndarray
    laugh: np.ndarray
    applause: np.ndarray
    cheer: np.ndarray


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def _fetch() -> None:
    import requests

    log("[hook] downloading the YAMNet sound event model (~16 MB, once)…")
    YAMNET_PATH.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(YAMNET_URL, timeout=60, stream=True) as r, atomic_path(YAMNET_PATH) as tmp:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for block in r.iter_content(1 << 20):
                f.write(block)
        if _sha256(tmp) != YAMNET_SHA256:
            raise ValueError("checksum mismatch")


def ready() -> bool:
    """Whether the sounds can be heard: onnxruntime is installed and the model is here with its pinned SHA-256
    (downloaded on first use)."""
    global _ready, _session
    if _ready is not None:
        return _ready
    try:
        import onnxruntime as ort

        if not YAMNET_PATH.is_file() or _sha256(YAMNET_PATH) != YAMNET_SHA256:
            _fetch()
        options = ort.SessionOptions()
        options.intra_op_num_threads = THREADS
        options.inter_op_num_threads = 1
        _session = ort.InferenceSession(str(YAMNET_PATH), options, providers=["CPUExecutionProvider"])
        _ready = True
    except Exception as exc:
        log(f"[hook] sound events unavailable ({type(exc).__name__}: {str(exc)[:120]}); laughter from the words only")
        _ready = False
    return _ready


def loudest(heards: list[Heard]) -> Heard:
    """The surest of several listenings of one video, frame by frame (the voice stem hears the laughter, the
    music/noise stem the applause and cheering)."""
    n = min(len(h.times) for h in heards)
    top = {name: np.max([getattr(h, name)[:n] for h in heards], axis=0) for name in ("laugh", "applause", "cheer")}
    return Heard(heards[0].times[:n], top["laugh"], top["applause"], top["cheer"])


def _scores(session, samples: np.ndarray) -> np.ndarray:
    return np.asarray(session.run(None, {"waveform": samples})[0], dtype=np.float32)


def listen(path: Path) -> Heard:
    """Every event of a whole file, decoded as a 16 kHz stream in chunks of two minutes (a long VOD never sits in
    memory); a chunk carries one frame hop more so its last frame is whole."""
    if not ready():
        raise RuntimeError("the sound event model is not available")
    step = int(round(FRAME_HOP * RATE))
    chunk = CHUNK_FRAMES * step
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(RATE),
           "-f", "s16le", "-"]
    total = probe_media(path)["duration"]
    rows, buf, rest = [], np.zeros(0, np.float32), b""
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        while data := proc.stdout.read(2 * (chunk + step - len(buf))):
            data, rest = rest + data, b""
            if len(data) % 2:
                data, rest = data[:-1], data[-1:]
            buf = np.concatenate([buf, np.frombuffer(data, np.int16) / np.float32(32768.0)])
            if len(buf) >= chunk + step:
                rows.append(_scores(_session, buf)[:CHUNK_FRAMES])
                buf = buf[chunk:]
                if total:
                    report(len(rows) * chunk / RATE / total)
    if len(buf) > step:
        rows.append(_scores(_session, np.pad(buf, (0, max(0, 2 * step - len(buf)))))[:len(buf) // step - 1 or 1])
    scores = np.concatenate(rows) if rows else np.zeros((0, 521), np.float32)
    times = (np.arange(len(scores)) + 1) * FRAME_HOP
    pick = {name: scores[:, list(idx)].max(axis=1) for name, idx in CLASSES.items()}
    return Heard(times, pick["laugh"], pick["applause"], pick["cheer"])
