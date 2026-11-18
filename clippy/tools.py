"""Shared helpers: project paths, ffmpeg (binary, features, probing, audio decoding), JSON, logging."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent


class ClippyError(RuntimeError):
    """User-facing error. With a catalogue `key` the browser shows the catalogue text; the message is the same."""

    def __init__(self, message: str = "", *, key: str | None = None, **params):
        from .i18n import t

        self.key, self.params = key, params
        super().__init__(t(key, **params) if key else message)


@lru_cache(maxsize=1)
def ffmpeg_path() -> str:
    """ffmpeg from FFMPEG_BINARY, PATH, or the bundled imageio-ffmpeg build."""
    env = os.environ.get("FFMPEG_BINARY")
    if env and Path(env).exists():
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise ClippyError("ffmpeg not found. Install it: pip install imageio-ffmpeg (or winget install Gyan.FFmpeg)") from exc


@lru_cache(maxsize=1)
def ffmpeg_features() -> dict:
    """Which optional ffmpeg features this build has."""
    ff = ffmpeg_path()
    filters = subprocess.run([ff, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    encoders = subprocess.run([ff, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return {
        "subtitles": bool(re.search(r"\bsubtitles\b", filters)),
        "nvenc": "h264_nvenc" in encoders and _nvenc_works(ff),
    }


def _nvenc_works(ff: str) -> bool:
    """h264_nvenc may be listed but unusable (no NVIDIA GPU / old driver)."""
    probe = subprocess.run(
        [ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=black:s=256x256:d=0.1",
         "-c:v", "h264_nvenc", "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0


@lru_cache(maxsize=16)
def probe_video(path: Path) -> dict:
    """Width, height, fps, duration, colour matrix/range and audio channels from `ffmpeg -i` (no ffprobe)."""
    out = subprocess.run([ffmpeg_path(), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stderr
    vline = re.search(r"Stream #\S+.*?Video: (.*)", out)
    if not vline:
        raise ClippyError(f"No video stream in {path}")
    line = vline.group(1)
    size = re.search(r"\b(\d{2,5})x(\d{2,5})\b", line)
    if not size:
        raise ClippyError(f"Cannot read the resolution: {line[:120]}")
    w, h = int(size.group(1)), int(size.group(2))
    rot = re.search(r"rotation of (-?\d+)", out) or re.search(r"rotate\s*:\s*(-?\d+)", out)
    if rot and abs(int(float(rot.group(1)))) in (90, 270):
        w, h = h, w
    fps_m = re.search(r"([\d.]+) fps", line) or re.search(r"([\d.]+) tbr", line)
    fps = float(fps_m.group(1)) if fps_m else 30.0
    dur_m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
    duration = int(dur_m.group(1)) * 3600 + int(dur_m.group(2)) * 60 + float(dur_m.group(3)) if dur_m else 0.0
    low = line.lower()
    if "bt2020" in low:
        matrix = "bt2020"
    elif "bt709" in low:
        matrix = "bt709"
    elif "bt470bg" in low or "smpte170m" in low or "bt601" in low:
        matrix = "bt601"
    else:
        matrix = "bt709" if h >= 720 else "bt601"
    rng = "pc" if re.search(r"\((?:pc|jpeg)[,)]", low) or "yuvj" in low else "tv"
    a = re.search(r"Audio: .*?(mono|stereo|5\.1|7\.1)", out)
    return {"width": w, "height": h, "fps": fps, "duration": duration, "matrix": matrix, "range": rng,
            "audio": a.group(1) if a else None}


def probe_media(path: Path) -> dict:
    """Streams and length of any media file from `ffmpeg -i`: {"video": (w, h) or None, "audio": bool, "duration"}."""
    out = subprocess.run([ffmpeg_path(), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stderr
    video = None
    vline = re.search(r"Stream #\S+.*?Video: (.*)", out)
    if vline and not re.search(r"\(attached pic\)", vline.group(1)):
        size = re.search(r"\b(\d{2,5})x(\d{2,5})\b", vline.group(1))
        video = (int(size.group(1)), int(size.group(2))) if size else None
    dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
    duration = int(dur.group(1)) * 3600 + int(dur.group(2)) * 60 + float(dur.group(3)) if dur else 0.0
    return {"video": video, "audio": bool(re.search(r"Stream #\S+.*?Audio: ", out)), "duration": duration}


def fps_rational(fps: float) -> tuple[str, float]:
    """Exact ffmpeg rate string for common NTSC rates; caps at 60 fps (social platforms)."""
    while fps > 60.5:
        fps /= 2
    for num, den in ((24000, 1001), (30000, 1001), (60000, 1001)):
        if abs(fps - num / den) < 0.01:
            return f"{num}/{den}", num / den
    r = round(fps)
    if abs(fps - r) < 0.02:
        return str(r), float(r)
    return f"{fps:.6f}", fps


def decode_audio(path: Path, sr: int = 16000, channels: int = 1, start: float | None = None,
                 duration: float | None = None) -> np.ndarray:
    """Decode audio with ffmpeg to float32: shape (samples,) for mono, (channels, samples) otherwise."""
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-i", str(path), "-vn", "-ac", str(channels), "-ar", str(sr), "-f", "f32le", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        raise ClippyError(f"Cannot decode the audio of {path}: {proc.stderr.decode(errors='replace')[-300:]}")
    audio = np.frombuffer(proc.stdout, np.float32).copy()
    return audio if channels == 1 else audio.reshape(-1, channels).T.copy()


def slugify(text: str, max_len: int = 50) -> str:
    """Filesystem-safe ASCII slug (accented letters folded: á -> a, ő -> o)."""
    table = str.maketrans("áéíóöőúüűÁÉÍÓÖŐÚÜŰ", "aeiooouuuAEIOOOUUU")
    text = text.translate(table).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return (text[:max_len].rstrip("-")) or "clip"


def safe_name(text: str, max_len: int = 80) -> str:
    """A readable Windows-safe folder/file name (keeps accents and spaces)."""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", text)
    text = re.sub(r"\s+", " ", text).strip().rstrip(". ")
    return text[:max_len].rstrip(". ") or "Untitled"


@contextmanager
def atomic_path(path: Path) -> Iterator[Path]:
    """A hidden temporary sibling that becomes `path` only when the block succeeds: readers see all or nothing."""
    path = Path(path)
    tmp = path.with_name(f".{path.stem}.{uuid.uuid4().hex[:8]}.part{path.suffix}")
    try:
        yield tmp
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    with atomic_path(path) as tmp:
        tmp.write_text(text, encoding="utf-8")


def move_atomic(src: Path, dst: Path) -> Path:
    """Rename (atomic on one drive); across drives copy to a temporary sibling first, then rename."""
    try:
        os.replace(src, dst)
    except OSError:
        with atomic_path(dst) as tmp:
            shutil.copy2(src, tmp)
        Path(src).unlink(missing_ok=True)
    return Path(dst)


def write_json(path: Path, data) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def json_cache(path: Path, make, decode=lambda data: data, encode=lambda value: value):
    """The value stored at `path`, else `make()` stored there; returns (value, made now)."""
    try:
        return decode(read_json(path)), False
    except (OSError, ValueError, TypeError):
        pass
    value = make()
    write_json(path, encode(value))
    return value, True


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_exact(stream, view: memoryview) -> int:
    """Fill `view` from a pipe without the extra copies of read(); returns the byte count (short at the end)."""
    got = 0
    while got < len(view):
        n = stream.readinto(view[got:])
        if not n:
            break
        got += n
    return got


def fmt_ts(seconds: float) -> str:
    m, s = divmod(max(0.0, seconds), 60)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{s:05.2f}" if h else f"{m}:{s:05.2f}"


_SINKS: dict[int, object] = {}


_STOPS: dict[int, threading.Event] = {}


def check_stop() -> None:
    """A job whose chat was deleted stops at its next log line or progress report (its worker threads too)."""
    stop = _STOPS.get(threading.get_ident())
    if stop is not None and stop.is_set():
        raise ClippyError(key="error.chat_deleted")


def current_stop() -> threading.Event | None:
    return _STOPS.get(threading.get_ident())


def log(msg: str) -> None:
    """Print progress; a thread may register a sink (web app jobs) that also receives every line."""
    check_stop()
    print(msg, file=sys.stderr, flush=True)
    sink = _SINKS.get(threading.get_ident())
    if sink is not None:
        try:
            sink(msg)
        except Exception:
            pass


class log_sink:
    """with log_sink(fn, on_report): ... , route this thread's log() lines (and report() fractions) to callbacks."""

    def __init__(self, fn, on_report=None, stop: threading.Event | None = None):
        self.fn, self.on_report, self.stop = fn, on_report, stop

    def __enter__(self):
        self.tid = threading.get_ident()
        _SINKS[self.tid] = self.fn
        if self.on_report is not None:
            _REPORTERS[self.tid] = self.on_report
        if self.stop is not None:
            _STOPS[self.tid] = self.stop
        return self

    def __exit__(self, *exc):
        _SINKS.pop(self.tid, None)
        _REPORTERS.pop(self.tid, None)
        _SPANS.pop(self.tid, None)
        _STOPS.pop(self.tid, None)
        return False


_REPORTERS: dict[int, object] = {}
_SPANS: dict[int, list[tuple[float, float]]] = {}


def report(fraction: float) -> None:
    """Fine-grained progress of the current step (0..1), mapped through the active report_span()s."""
    check_stop()
    tid = threading.get_ident()
    fn = _REPORTERS.get(tid)
    if fn is None:
        return
    lo, hi = (_SPANS.get(tid) or [(0.0, 1.0)])[-1]
    try:
        fn(lo + (hi - lo) * min(1.0, max(0.0, float(fraction))))
    except Exception:
        pass


@dataclass
class ThreadContext:
    """The log sink, progress reporter and span of one thread, to be carried into worker threads."""

    sink: Callable | None = None
    reporter: Callable | None = None
    spans: list[tuple[float, float]] = field(default_factory=list)
    stop: threading.Event | None = None

    def wrap(self, fn: Callable) -> Callable:
        def run(*args, **kwargs):
            tid = threading.get_ident()
            if self.sink is not None:
                _SINKS[tid] = self.sink
            if self.reporter is not None:
                _REPORTERS[tid] = self.reporter
            _SPANS[tid] = list(self.spans)
            if self.stop is not None:
                _STOPS[tid] = self.stop
            try:
                return fn(*args, **kwargs)
            finally:
                _SINKS.pop(tid, None)
                _REPORTERS.pop(tid, None)
                _SPANS.pop(tid, None)
                _STOPS.pop(tid, None)
        return run


def thread_context() -> ThreadContext:
    tid = threading.get_ident()
    return ThreadContext(_SINKS.get(tid), _REPORTERS.get(tid), list(_SPANS.get(tid) or []), _STOPS.get(tid))


class SharedProgress:
    """Parts of one step that run in parallel threads: the caller's progress shows the mean of their fractions."""

    def __init__(self, parts: int):
        self.parent = thread_context()
        self.done = [0.0] * max(1, parts)
        self._lock = threading.Lock()

    def wrap(self, part: int, fn: Callable) -> Callable:
        return ThreadContext(self.parent.sink, lambda f: self._update(part, f)).wrap(fn)

    def _update(self, part: int, fraction: float) -> None:
        with self._lock:
            self.done[part] = max(self.done[part], fraction)
            mean = sum(self.done) / len(self.done)
        if self.parent.reporter is not None:
            lo, hi = self.parent.spans[-1] if self.parent.spans else (0.0, 1.0)
            self.parent.reporter(lo + (hi - lo) * mean)


class report_span:
    """with report_span(lo, hi): report(0..1) inside maps onto lo..hi of the enclosing span."""

    def __init__(self, lo: float, hi: float):
        self.lo, self.hi = lo, hi

    def __enter__(self):
        tid = threading.get_ident()
        stack = _SPANS.setdefault(tid, [])
        plo, phi = stack[-1] if stack else (0.0, 1.0)
        stack.append((plo + (phi - plo) * self.lo, plo + (phi - plo) * self.hi))
        report(0.0)
        return self

    def __exit__(self, *exc):
        report(1.0)
        stack = _SPANS.get(threading.get_ident())
        if stack:
            stack.pop()
        return False
