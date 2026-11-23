"""Voice isolation with Demucs (voice / music stems) and the optional music-ducked mix."""

from __future__ import annotations

import importlib.util
import subprocess
from contextlib import nullcontext
from pathlib import Path

import numpy as np

from .tools import ClippyError, atomic_path, decode_audio, ffmpeg_failed, ffmpeg_path, log, report

SR = 44100


def available() -> bool:
    return all(importlib.util.find_spec(m) is not None for m in ("torch", "demucs"))


def _writer(path: Path):
    return subprocess.Popen([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", "-f", "f32le", "-ac", "2",
                             "-ar", str(SR), "-i", "-", "-c:a", "flac", str(path)], stdin=subprocess.PIPE)


def separate(video: Path, cache_dir: Path, chunk_sec: float = 600.0, pad_sec: float = 8.0,
             gpu=nullcontext, sound: Path | None = None) -> dict[str, Path]:
    """-> {"vocals": flac, "music": flac} (44.1 kHz stereo), cached by the video's name; both stems appear together
    or not at all. `sound`: decode this file instead (the audio-only download while the video is still coming)."""
    vocals_p = cache_dir / f"{video.stem}.vocals.flac"
    music_p = cache_dir / f"{video.stem}.music.flac"
    if vocals_p.exists() and music_p.exists():
        log("[audio] voice/music separation: cache")
        return {"vocals": vocals_p, "music": music_p}
    with gpu(), atomic_path(vocals_p) as vocals_tmp, atomic_path(music_p) as music_tmp:
        _separate(sound or video, vocals_tmp, music_tmp, chunk_sec, pad_sec)
    return {"vocals": vocals_p, "music": music_p}


def _separate(video: Path, vocals_p: Path, music_p: Path, chunk_sec: float, pad_sec: float) -> None:
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    device = "cuda" if torch.cuda.is_available() else "cpu"
    log(f"[audio] isolating the voice from music/noise (Demucs htdemucs, {device})…")
    model = get_model("htdemucs")
    model.to(device).eval()
    vi = model.sources.index("vocals")

    wav = decode_audio(video, sr=SR, channels=2)
    n = wav.shape[1]
    C, P, F = int(chunk_sec * SR), int(pad_sec * SR), int(0.05 * SR)
    wv, wm = _writer(vocals_p), _writer(music_p)
    prev_tail = None
    try:
        for cs in range(0, n, C):
            ce = min(n, cs + C)
            ws, we = max(0, cs - P), min(n, ce + P)
            seg = torch.from_numpy(wav[:, ws:we])
            ref = seg.mean(0)
            mean, std = ref.mean(), ref.std() + 1e-8
            with torch.inference_mode():
                out = apply_model(model, ((seg - mean) / std)[None].to(device), device=device, shifts=1,
                                  split=True, overlap=0.25, progress=False)[0]
            out = (out * std + mean).cpu().numpy()
            voc = out[vi]
            mus = out.sum(0) - voc
            stems = np.stack([voc, mus])
            core = stems[:, :, cs - ws: ce - ws].copy()
            if prev_tail is not None:
                f = min(F, core.shape[2], prev_tail.shape[2])
                w = np.linspace(0.0, 1.0, f, dtype=np.float32)
                core[:, :, :f] = prev_tail[:, :, :f] * (1 - w) + core[:, :, :f] * w
            prev_tail = stems[:, :, ce - ws: ce - ws + F]
            wv.stdin.write(np.ascontiguousarray(core[0].T).astype(np.float32).tobytes())
            wm.stdin.write(np.ascontiguousarray(core[1].T).astype(np.float32).tobytes())
            log(f"[audio] separation {ce / SR / 60:.1f}/{n / SR / 60:.1f} min")
            report(ce / n)
    finally:
        for w in (wv, wm):
            w.stdin.close()
            w.wait()
    if any(w.returncode for w in (wv, wm)):
        raise ClippyError("Writing the separated stems failed.")
    del model
    if device == "cuda":
        torch.cuda.empty_cache()


def ducked_mix(stems: dict[str, Path], cache_dir: Path, video: Path, duck_db: float) -> Path:
    """Voice at 0 dB + music/FX lowered by duck_db: dialogue-forward mix for phone speakers."""
    out = cache_dir / f"{video.stem}.duck{int(round(duck_db))}.flac"
    if out.exists():
        return out
    with atomic_path(out) as tmp:
        cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(stems["vocals"]),
               "-i", str(stems["music"]), "-filter_complex",
               f"[1:a]volume=-{duck_db:.1f}dB[m];[0:a][m]amix=inputs=2:normalize=0:dropout_transition=0[a]",
               "-map", "[a]", "-c:a", "flac", str(tmp)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise ffmpeg_failed("mixing the voice and the music", proc.stderr)
    return out
