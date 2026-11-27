"""An optional look at the picture of the reaction peaks where little is said (gameplay, a stream's big moment): a
local vision model sees a few frames around each peak and tells what happens there, so the global rerank weighs
what is on screen next to the sound, the motion and the chat. Off unless CLIPPY_VISION_MODEL names an installed
Ollama model; a failure only leaves the marks out.

With one model in VRAM every switch between the text model and the vision model is a reload, so all looks of a job
run together, before the first text call, in one hold of the GPU slot: the vision model loads once and the text
model once after it."""

from __future__ import annotations

import base64
import os
import re
import subprocess
import time
from dataclasses import dataclass, field, fields
from pathlib import Path

import requests

from .concurrency import GPU
from .llm import OLLAMA_URL, OllamaLLM, parse_json_reply, setting
from .signals import Signals
from .tools import check_stop, ffmpeg_path, log, report

FRAMES = (-3.0, -1.0, 0.5, 2.0)
WIDTH = 448
MAX_LOOKS = 8
PEAKS = 30
AROUND = 15.0
SPARSE = 0.5
TIMEOUT = 300
CONTEXT = 8192
MAX_WHAT = 80
CONTROL = re.compile(r"[\x00-\x1f\x7f  ⟪⟫|]")

PROMPT = """You see {n} frames of a video ("{title}"), taken {times} seconds around a moment where the {kinds}
suddenly reacts. Little is said there, so the picture decides whether it makes a short clip.
Does something visually notable happen: an explosion, a crash, a kill, a goal, a win or a fail, a jump scare, a stunt,
a big reaction on the face cam, something funny or surprising? A static scene, a menu, a loading or talking-head
screen with nothing happening is not notable.
"score": 0 (nothing happens) to 10 (a moment people would share). "what": what happens, at most 12 English words.
Return ONLY JSON: {{"score": <0-10>, "what": "<what happens>"}}"""

SCHEMA = {
    "type": "object",
    "properties": {"score": {"type": "integer"}, "what": {"type": "string"}},
    "required": ["score", "what"],
}

KIND_NAMES = {"loud": "sound", "action": "picture", "chat": "chat", "laughter": "laughter"}


def model() -> str:
    return os.environ.get("CLIPPY_VISION_MODEL", "").strip()


@dataclass(frozen=True)
class Verdict:
    time: float
    score: int
    what: str
    seconds: float = 0.0

    def mark(self) -> str:
        return f"on screen {self.score}/10: {self.what}"


def verdict(time_: float, reply) -> Verdict | None:
    """The model's answer kept only when it has the asked shape: a whole score 0..10 and a short one-line text."""
    if not isinstance(reply, dict):
        return None
    score, what = reply.get("score"), reply.get("what")
    if isinstance(score, float) and score.is_integer():
        score = int(score)
    if isinstance(score, bool) or not isinstance(score, int) or not isinstance(what, str):
        return None
    what = " ".join(CONTROL.sub(" ", what).split())[:MAX_WHAT].strip()
    if not what:
        return None
    return Verdict(round(time_, 1), max(0, min(10, score)), what)


@dataclass
class Sighted(Signals):
    """Signals that also tell, for a span, what the vision model saw at its peaks."""

    verdicts: list[Verdict] = field(default_factory=list)

    def describe(self, start: float, end: float) -> str:
        seen = "; ".join(v.mark() for v in self.verdicts if start <= v.time <= end)
        return "; ".join(x for x in (super().describe(start, end), seen) if x)


def moments(signals: Signals, words: list[dict], limit: int = MAX_LOOKS) -> list[tuple[float, tuple[str, ...]]]:
    """The strongest reaction peaks where less than half of the time around them is speech."""
    out = []
    for peak in signals.peaks(PEAKS):
        a, b = peak.time - AROUND, peak.time + AROUND
        said = sum(max(0.0, min(w["end"], b) - max(w["start"], a)) for w in words if w["end"] > a and w["start"] < b)
        if said < SPARSE * (b - a):
            out.append((peak.time, peak.kinds))
        if len(out) >= limit:
            break
    return out


def frame(video: Path, at: float, width: int = WIDTH) -> str:
    """One frame at `at` seconds as a base64 JPEG, `width` pixels wide."""
    proc = subprocess.run([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, at):.2f}",
                           "-i", str(video), "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4",
                           "-f", "image2pipe", "-c:v", "mjpeg", "-"], capture_output=True, timeout=60)
    if proc.returncode or not proc.stdout:
        raise OSError(f"no frame at {at:.1f} s")
    return base64.b64encode(proc.stdout).decode("ascii")


class VisionLLM(OllamaLLM):
    """The vision model on the local Ollama server; `loaded` adds up the seconds Ollama spent loading it. A request
    gives up after TIMEOUT seconds (a busy GPU), so a job waits for it at most once."""

    def __init__(self, name: str):
        base = setting("BASE_URL", OLLAMA_URL) if setting("BACKEND", "ollama").lower() == "ollama" else OLLAMA_URL
        super().__init__(name, base)
        self.loaded = 0.0

    def see(self, prompt: str, images: list[str]) -> dict:
        payload = {"model": self.model, "stream": False, "format": SCHEMA, "keep_alive": "5m",
                   "messages": [{"role": "user", "content": prompt, "images": images}],
                   "options": {"temperature": 0.2, "num_predict": 200, "num_ctx": CONTEXT}}
        r = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=TIMEOUT)
        if not r.ok:
            raise OSError(f"vision model answered {r.status_code}: {r.text[:200]}")
        body = r.json()
        self.loaded += float(body.get("load_duration") or 0) / 1e9
        return parse_json_reply(str((body.get("message") or {}).get("content") or ""))


def look(signals: Signals | None, video: Path | None, words: list[dict], title: str = "",
         seer: VisionLLM | None = None) -> Signals | None:
    """`signals` with what the vision model saw at the quiet reaction peaks, or `signals` unchanged when the check is
    off, has nothing to look at or fails."""
    name = model()
    if signals is None or not (name or seer) or video is None or not Path(video).is_file():
        return signals
    looks = moments(signals, words)
    if not looks:
        return signals
    seer = seer or VisionLLM(name)
    problem = seer.problem()
    if problem is not None:
        log(f"[vision] skipped: {problem}")
        return signals
    found: list[Verdict] = []
    began = time.perf_counter()
    with GPU.hold():
        for n, (at, kinds) in enumerate(looks):
            report(n / len(looks))
            started = time.perf_counter()
            try:
                images = [frame(Path(video), at + d) for d in FRAMES]
                prompt = PROMPT.format(n=len(images), title=title.replace('"', "'")[:100],
                                       times=", ".join(f"{d:+g}" for d in FRAMES),
                                       kinds=" and ".join(KIND_NAMES.get(k, k) for k in kinds) or "sound")
                seen = verdict(at, seer.see(prompt, images))
            except Exception as exc:
                check_stop()
                log(f"[vision] stopped looking ({type(exc).__name__}: {str(exc)[:120]})")
                break
            if seen is not None:
                seen = Verdict(seen.time, seen.score, seen.what, round(time.perf_counter() - started, 2))
                found.append(seen)
                log(f"[vision] {at:.0f} s: {seen.mark()} ({seen.seconds:.1f} s)")
        seer.unload()
    report(1.0)
    if not found:
        return signals
    log(f"[vision] {len(found)} peaks seen in {time.perf_counter() - began:.0f} s "
        f"(model load {seer.loaded:.0f} s)")
    return Sighted(**{f.name: getattr(signals, f.name) for f in fields(Signals)}, verdicts=found)
