"""Shared helpers: paths, ffmpeg (binary, features, probing, decoding), atomic files, JSON, job logs and progress."""

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
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
PROBE_TIMEOUT = 60
FRAME_PIPE = 1 << 20


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
        raise ClippyError(key="error.ffmpeg_missing") from exc


@lru_cache(maxsize=1)
def ffmpeg_features() -> dict:
    """Which optional ffmpeg features this build has; the GPU ones are tried at the same time (each starts CUDA)."""
    ff = ffmpeg_path()
    filters, encoders, hwaccels = (
        subprocess.run([ff, "-hide_banner", flag], capture_output=True, text=True, timeout=PROBE_TIMEOUT).stdout
        for flag in ("-filters", "-encoders", "-hwaccels"))
    tries = {"nvenc": "h264_nvenc" in encoders and
             ["-f", "lavfi", "-i", "color=black:s=256x256:d=0.1", "-c:v", "h264_nvenc"],
             "nvdec": bool(re.search(r"^cuda$", hwaccels, re.M) and re.search(r"\bscale_cuda\b", filters)) and
             ["-init_hw_device", "cuda=gpu", "-filter_hw_device", "gpu", "-f", "lavfi", "-i",
              "color=black:s=256x256:d=0.1", "-vf",
              "format=nv12,hwupload,scale_cuda=format=yuv420p,hwdownload,format=yuv420p"]}
    with ThreadPoolExecutor(max_workers=len(tries)) as pool:
        works = {name: pool.submit(_ffmpeg_ok, ff, args) for name, args in tries.items() if args}
        return {"subtitles": bool(re.search(r"\bsubtitles\b", filters)),
                **{name: name in works and works[name].result() for name in tries}}


def _ffmpeg_ok(ff: str, args: list[str]) -> bool:
    """A GPU feature may be listed but unusable (no NVIDIA GPU, an old or hung driver): a tiny run tells."""
    try:
        probe = subprocess.run([ff, "-hide_banner", "-loglevel", "error", *args, "-f", "null", "-"],
                               capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


GPU_BUSY = 50


def gpu_busy() -> bool:
    """Whether the NVIDIA GPU is busy computing (Whisper, a local LLM, another program): NVDEC then waits its turn
    for every frame and decodes several times slower than the CPU."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return False
    try:
        out = subprocess.run([smi, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return True
    loads = [int(v) for v in re.findall(r"\d+", out)]
    return not loads or max(loads) >= GPU_BUSY


def _ffmpeg_info(path: Path) -> str:
    """What `ffmpeg -i` prints about a file: its streams and duration (the bundled build has no ffprobe)."""
    try:
        return subprocess.run([ffmpeg_path(), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=PROBE_TIMEOUT).stderr
    except subprocess.TimeoutExpired as exc:
        raise ClippyError(key="error.probe_failed", name=Path(path).name) from exc


def _duration(info: str) -> float:
    found = re.search(r"Duration: (\d+):(\d+):([\d.]+)", info)
    return int(found.group(1)) * 3600 + int(found.group(2)) * 60 + float(found.group(3)) if found else 0.0


@lru_cache(maxsize=16)
def probe_video(path: Path) -> dict:
    """Width, height, fps, duration, colour matrix/range and audio channels of a video."""
    out = _ffmpeg_info(path)
    vline = re.search(r"Stream #\S+.*?Video: (.*)", out)
    if not vline:
        raise ClippyError(key="error.no_video_stream", name=Path(path).name)
    line = vline.group(1)
    size = re.search(r"\b(\d{2,5})x(\d{2,5})\b", line)
    if not size:
        raise ClippyError(key="error.no_resolution", name=Path(path).name, detail=line[:120])
    w, h = int(size.group(1)), int(size.group(2))
    rot = re.search(r"rotation of (-?\d+)", out) or re.search(r"rotate\s*:\s*(-?\d+)", out)
    if rot and abs(int(float(rot.group(1)))) in (90, 270):
        w, h = h, w
    fps_m = re.search(r"([\d.]+) fps", line) or re.search(r"([\d.]+) tbr", line)
    fps = float(fps_m.group(1)) if fps_m else 30.0
    duration = _duration(out)
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
    codec = re.match(r"(\w+)", line)
    pix = re.match(r"\S+(?: \([^)]*\))*, (\w+)", line)
    return {"width": w, "height": h, "fps": fps, "duration": duration, "matrix": matrix, "range": rng,
            "audio": a.group(1) if a else None, "codec": codec.group(1) if codec else "",
            "pix_fmt": pix.group(1) if pix else "", "rotated": bool(rot and int(float(rot.group(1))) % 360)}


def probe_media(path: Path) -> dict:
    """Streams and length of any media file: {"video": (w, h) or None, "audio": bool, "duration"}."""
    out = _ffmpeg_info(path)
    video = None
    vline = re.search(r"Stream #\S+.*?Video: (.*)", out)
    if vline and not re.search(r"\(attached pic\)", vline.group(1)):
        size = re.search(r"\b(\d{2,5})x(\d{2,5})\b", vline.group(1))
        video = (int(size.group(1)), int(size.group(2))) if size else None
    return {"video": video, "audio": bool(re.search(r"Stream #\S+.*?Audio: ", out)), "duration": _duration(out)}


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
        raise ClippyError(key="error.audio_decode", name=Path(path).name,
                          detail=proc.stderr.decode(errors="replace")[-300:])
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


def frame_pipe() -> tuple[int, int]:
    """A pipe for raw frames as (read fd, write fd). The pipes subprocess makes on Windows hold 4 KB, and moving 4K
    frames through them takes longer than decoding them; a 1 MB pipe moves them about twice as fast."""
    if sys.platform != "win32":
        return os.pipe()
    import _winapi
    import msvcrt

    r, w = _winapi.CreatePipe(None, FRAME_PIPE)
    return msvcrt.open_osfhandle(r, os.O_RDONLY), msvcrt.open_osfhandle(w, 0)


def spawn_piped(cmd: list[str], out: bool, **kw) -> tuple[subprocess.Popen, BinaryIO]:
    """Start `cmd` with its stdout (`out`) or stdin on a frame pipe; returns the process and our end of the pipe."""
    r, w = frame_pipe()
    ours, theirs = (r, w) if out else (w, r)
    try:
        proc = subprocess.Popen(cmd, **({"stdout": theirs} if out else {"stdin": theirs}), **kw)
    except BaseException:
        os.close(ours)
        raise
    finally:
        os.close(theirs)
    return proc, open(ours, "rb", buffering=0) if out else open(ours, "wb")


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


_SINKS: dict[int, Callable] = {}
_REPORTERS: dict[int, Callable] = {}
_SPANS: dict[int, list[tuple[float, float]]] = {}
_STOPS: dict[int, threading.Event] = {}


def check_stop() -> None:
    """A job whose chat was deleted stops at its next log line or progress report (its worker threads too)."""
    stop = _STOPS.get(threading.get_ident())
    if stop is not None and stop.is_set():
        raise ClippyError(key="error.chat_deleted")


def current_stop() -> threading.Event | None:
    return _STOPS.get(threading.get_ident())


def _notify(callback: Callable, value) -> None:
    """A failing progress callback never stops the job; its error goes to the server log."""
    try:
        callback(value)
    except Exception as exc:
        print(f"[progress] callback failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


OUT_OF_MEMORY = ("cannot allocate memory", "out of memory", "error code: -12")


def ffmpeg_failed(step: str, stderr: str) -> "ClippyError":
    """An ffmpeg run that failed: its own words go to the log, the user gets what happened and what helps."""
    tail = "\n".join(stderr.strip().splitlines()[-15:])
    log(f"[ffmpeg] {step} failed:\n{tail}")
    memory = any(m in tail.lower() for m in OUT_OF_MEMORY)
    return ClippyError(key="error.ffmpeg_memory" if memory else "error.ffmpeg_failed", step=step)


def log(msg: str) -> None:
    """Print progress; a thread may register a sink (web app jobs) that also receives every line."""
    check_stop()
    print(msg, file=sys.stderr, flush=True)
    sink = _SINKS.get(threading.get_ident())
    if sink is not None:
        _notify(sink, msg)


def report(fraction: float) -> None:
    """Fine-grained progress of the current step (0..1), mapped through the active report_span()s."""
    check_stop()
    tid = threading.get_ident()
    fn = _REPORTERS.get(tid)
    if fn is None:
        return
    lo, hi = (_SPANS.get(tid) or [(0.0, 1.0)])[-1]
    _notify(fn, lo + (hi - lo) * min(1.0, max(0.0, float(fraction))))


@dataclass
class ThreadContext:
    """The log sink, progress reporter, spans and stop event of one thread, to be carried into worker threads."""

    sink: Callable | None = None
    reporter: Callable | None = None
    spans: list[tuple[float, float]] = field(default_factory=list)
    stop: threading.Event | None = None

    def install(self) -> int:
        """Makes this the calling thread's context; returns the thread's id for `clear`."""
        tid = threading.get_ident()
        for registry, value in ((_SINKS, self.sink), (_REPORTERS, self.reporter), (_STOPS, self.stop)):
            if value is not None:
                registry[tid] = value
        _SPANS[tid] = list(self.spans)
        return tid

    @staticmethod
    def clear(tid: int) -> None:
        for registry in (_SINKS, _REPORTERS, _SPANS, _STOPS):
            registry.pop(tid, None)

    def wrap(self, fn: Callable) -> Callable:
        def run(*args, **kwargs):
            tid = self.install()
            try:
                return fn(*args, **kwargs)
            finally:
                self.clear(tid)
        return run


class log_sink:
    """with log_sink(fn, on_report, stop): this thread's log() lines and report() fractions go to the callbacks."""

    def __init__(self, fn, on_report=None, stop: threading.Event | None = None):
        self.context = ThreadContext(fn, on_report, stop=stop)

    def __enter__(self):
        self.tid = self.context.install()
        return self

    def __exit__(self, *exc):
        ThreadContext.clear(self.tid)
        return False


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
        """`fn` run as one part: its lines go to the caller's log, and a stopped job stops it too."""
        return ThreadContext(self.parent.sink, lambda f: self._update(part, f), stop=self.parent.stop).wrap(fn)

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
