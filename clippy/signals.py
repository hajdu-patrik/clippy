"""Reactions over the whole video, read without the words: loud moments (a scream, an explosion, cheering), laughter,
applause and cheering heard in the sound, sudden action on screen and chat bursts of a stream replay. They mark the
peaks of gameplay and streams, where the best moment is often not something said."""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .tools import ffmpeg_path, json_cache, log, report, report_span

VERSION = 2
HOP = 0.5
RATE = 8000
BASELINE_SEC = 90.0
SILENT_DB = -50.0
LAUGH = re.compile(r"\b((h[ae]){2,}h?|lo+l|lmao|rofl|xd+|nevet\w*|kacag\w*)\b|\(laugh|\[laugh|laughs\b|laughter",
                   re.I)
PEAK_LEVEL = 0.45
PEAK_GAP = 25.0
TAG_LEVEL = 0.35
CHAT_QUOTES = 3
KEY_SAMPLE_SEC, DENSE_KEYS = 30, 8
HEARD_FLOOR, HEARD_FULL = 0.1, 0.4
HEARD = (("laughter", "laughter"), ("applause", "applause"), ("cheer", "cheering"))


def rolling_median(values: np.ndarray, window: int) -> np.ndarray:
    """The local normal level around every point (a stream gets louder and quieter over hours)."""
    if len(values) == 0:
        return values
    half = max(1, window // 2)
    padded = np.pad(values, half, mode="reflect" if len(values) > half else "edge")
    step = max(1, window // 8)
    centres = np.arange(0, len(values), step)
    medians = np.array([np.median(padded[c:c + 2 * half + 1]) for c in centres])
    return np.interp(np.arange(len(values)), centres, medians)


def rises(values: np.ndarray, hop: float = HOP, floor: float = 3.0, start: float = 1.5, span: float = 2.5) -> np.ndarray:
    """0..1 for how far each point rises above its own surroundings (robust: median and median deviation)."""
    if len(values) == 0:
        return values
    base = rolling_median(values, int(BASELINE_SEC / hop))
    spread = max(floor, 1.4826 * float(np.median(np.abs(values - base))))
    return np.clip(((values - base) / spread - start) / span, 0.0, 1.0)


def loudness_db(path: Path, hop: float = HOP) -> np.ndarray:
    """RMS level in dBFS every `hop` seconds of a whole file, decoded as a stream (a long VOD never sits in memory)."""
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(RATE),
           "-f", "s16le", "-"]
    block = int(RATE * hop)
    levels, rest = [], b""
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        while chunk := proc.stdout.read(block * 2 * 240):
            data = rest + chunk
            usable = len(data) // (block * 2) * block * 2
            rest = data[usable:]
            samples = np.frombuffer(data[:usable], np.int16).astype(np.float32) / 32768.0
            rms = np.sqrt(np.mean(samples.reshape(-1, block) ** 2, axis=1) + 1e-12)
            levels.append(20 * np.log10(rms + 1e-9))
    return np.concatenate(levels) if levels else np.zeros(0)


def keyframe_motion(video: Path) -> list[tuple[float, float]]:
    """(time, change) between the video's keyframes, tiny and grey: only the keyframes are decoded, so a long VOD
    takes seconds. A sudden change is a cut, a flash, an explosion or a fast turn."""
    w, h = 64, 36

    def run(*limit: str) -> subprocess.CompletedProcess:
        return subprocess.run([ffmpeg_path(), "-hide_banner", "-skip_frame", "nokey", *limit, "-i", str(video), "-an",
                               "-vf", f"scale={w}:{h},format=gray,showinfo", "-fps_mode", "vfr", "-f", "rawvideo", "-"],
                              capture_output=True)

    sample = len(re.findall(rb"pts_time:", run("-t", str(KEY_SAMPLE_SEC)).stderr))
    if sample > KEY_SAMPLE_SEC * DENSE_KEYS:
        log("[hook] motion skipped: this video has a keyframe in nearly every frame (too slow to read)")
        return []
    proc = run()
    times = [float(m) for m in re.findall(rb"pts_time:([\d.]+)", proc.stderr)]
    frames = np.frombuffer(proc.stdout, np.uint8)
    count = min(len(times), len(frames) // (w * h))
    if count < 3:
        return []
    frames = frames[:count * w * h].reshape(count, h, w).astype(np.float32)
    change = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))
    return list(zip(times[1:count], change.tolist()))


def chat_messages(path: Path) -> list[tuple[float, str]]:
    """(seconds, text) of a YouTube live chat replay as yt-dlp saves it (one JSON action per line); what was written
    before the stream began (offset 0) is left out."""
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            action = json.loads(line)
            replay = action["replayChatItemAction"]
            offset = int(replay["videoOffsetTimeMsec"]) / 1000.0
        except (ValueError, KeyError, TypeError):
            continue
        if offset <= 0:
            continue
        for item in replay.get("actions") or []:
            renderer = ((item.get("addChatItemAction") or {}).get("item") or {}).get("liveChatTextMessageRenderer")
            if not renderer:
                continue
            runs = (renderer.get("message") or {}).get("runs") or []
            text = "".join(r.get("text") or ((r.get("emoji") or {}).get("shortcuts") or [""])[0] for r in runs)
            out.append((offset, " ".join(text.split())[:80]))
    return out


def curve(points: list[tuple[float, float]], length: int, hop: float = HOP) -> np.ndarray:
    """Point values spread onto the hop grid (the highest value in each hop)."""
    grid = np.zeros(length)
    for t, value in points:
        i = int(t / hop)
        if 0 <= i < length:
            grid[i] = max(grid[i], value)
    return grid


@dataclass(frozen=True)
class Peak:
    time: float
    strength: float
    kinds: tuple[str, ...]


@dataclass
class Signals:
    hop: float = HOP
    loud: list[float] = field(default_factory=list)
    motion: list[float] = field(default_factory=list)
    chat: list[float] = field(default_factory=list)
    chat_rate: list[float] = field(default_factory=list)
    laughs: list[float] = field(default_factory=list)
    laughter: list[float] = field(default_factory=list)
    applause: list[float] = field(default_factory=list)
    cheer: list[float] = field(default_factory=list)
    chat_lines: list[tuple[float, str]] = field(default_factory=list)

    @property
    def length(self) -> int:
        return max(len(self.loud), len(self.motion), len(self.chat), len(self.laughter), len(self.applause),
                   len(self.cheer))

    def _arr(self, name: str) -> np.ndarray:
        values = np.zeros(self.length)
        got = np.asarray(getattr(self, name), dtype=float)
        values[:len(got)] = got
        return values

    def strength(self) -> np.ndarray:
        """How much happens at each hop, 0..1: the strongest reaction, a bit more when several come together."""
        loud, motion, chat = self._arr("loud"), self._arr("motion"), self._arr("chat")
        laugh = np.maximum(curve([(t, 1.0) for t in self.laughs], self.length, self.hop), self._arr("laughter"))
        crowd = np.maximum(self._arr("applause"), self._arr("cheer"))
        parts = np.vstack([loud, 0.6 * motion, chat, 0.7 * laugh, 0.8 * crowd])
        top = parts.max(axis=0)
        together = (parts > TAG_LEVEL).sum(axis=0)
        return np.clip(top + 0.12 * np.maximum(0, together - 1), 0.0, 1.0)

    def peaks(self, limit: int = 30, gap: float = PEAK_GAP) -> list[Peak]:
        """The strongest moments, at least `gap` seconds apart, strongest first."""
        if not self.length:
            return []
        smooth = np.convolve(self.strength(), np.array([0.25, 0.5, 0.25]), mode="same")
        out: list[Peak] = []
        for i in np.argsort(-smooth):
            if smooth[i] < PEAK_LEVEL or len(out) >= limit:
                break
            t = float(i * self.hop)
            if all(abs(t - p.time) >= gap for p in out):
                out.append(Peak(t, round(float(smooth[i]), 3), self.kinds(t - 2.0, t + 2.0)))
        return out

    def kinds(self, start: float, end: float) -> tuple[str, ...]:
        a, b = max(0, int(start / self.hop)), max(1, int(end / self.hop) + 1)
        found = []
        for name, values in (("loud", self.loud), ("action", self.motion), ("chat", self.chat)):
            if values and max(values[a:b] or [0.0]) >= TAG_LEVEL:
                found.append(name)
        for name, kind in HEARD:
            values = getattr(self, name)
            said = name == "laughter" and any(start <= t <= end for t in self.laughs)
            if said or (values and max(values[a:b] or [0.0]) >= TAG_LEVEL):
                found.append(kind)
        return tuple(found)

    def tags(self, start: float, end: float) -> str:
        """Short marks for one transcript line: "loud, chat x4"."""
        out = []
        for kind in self.kinds(start, end):
            if kind == "chat":
                a, b = max(0, int(start / self.hop)), int(end / self.hop) + 1
                rate = max(self.chat_rate[a:b] or [1.0])
                out.append(f"chat x{max(2, round(rate))}")
            else:
                out.append(kind)
        return ", ".join(out)

    def describe(self, start: float, end: float) -> str:
        """For the editor comparing clips: the reactions in a span and what the chat wrote then."""
        marks = self.tags(start, end)
        said = Counter(text for t, text in self.chat_lines if start <= t <= end + 5 and 0 < len(text) <= 40)
        quotes = ", ".join(f'"{text}"' for text, _ in said.most_common(CHAT_QUOTES))
        return "; ".join(x for x in (marks, f"chat wrote {quotes}" if quotes else "") if x)

    def to_json(self) -> dict:
        data = asdict(self)
        data["chat_lines"] = [list(x) for x in self.chat_lines]
        return data

    @classmethod
    def from_json(cls, data: dict) -> "Signals":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        known["chat_lines"] = [tuple(x) for x in known.get("chat_lines") or []]
        return cls(**known)


def heard_levels(heard, length: int, hop: float = HOP) -> dict[str, list[float]]:
    """The sound event model's frames on the hop grid, 0..1: nothing below HEARD_FLOOR, sure from HEARD_FULL."""
    out = {}
    for name, values in (("laughter", heard.laugh), ("applause", heard.applause), ("cheer", heard.cheer)):
        level = np.clip((np.asarray(values) - HEARD_FLOOR) / (HEARD_FULL - HEARD_FLOOR), 0.0, 1.0)
        grid = curve(list(zip(np.asarray(heard.times).tolist(), level.tolist())), length, hop)
        out[name] = np.round(grid, 3).tolist()
    return out


def _hear(out: Signals, heard: tuple[Path, ...], length: int, hop: float) -> int:
    from . import audio_events

    listened = []
    try:
        for i, path in enumerate(heard):
            with report_span(0.15 + 0.35 * i / len(heard), 0.15 + 0.35 * (i + 1) / len(heard)):
                listened.append(audio_events.listen(path))
    except (OSError, RuntimeError, ValueError) as exc:
        log(f"[hook] sound events skipped ({type(exc).__name__}: {str(exc)[:120]})")
        return length
    found = audio_events.loudest(listened)
    if not length and len(found.times):
        length = int(found.times[-1] / hop) + 1
    for name, values in heard_levels(found, length, hop).items():
        setattr(out, name, values)
    return length


def build(sound: Path | None, words: list[dict], video: Path | None = None, chat: Path | None = None,
          hop: float = HOP, heard: tuple[Path, ...] = ()) -> Signals:
    """Every reaction this video has: the sound always, the picture and the chat when they are there, and the
    laughter, applause and cheering in `heard` (the voice and the music/noise stems, else the whole mix) when the sound
    event model is here."""
    out = Signals(hop=hop, laughs=[w["start"] for w in words if LAUGH.search(w.get("word") or "")])
    if sound is not None:
        report(0.1)
        db = loudness_db(sound, hop)
        loud = rises(db, hop)
        loud[db < SILENT_DB] = 0.0
        out.loud = np.round(loud, 3).tolist()
    length = len(out.loud)
    if heard:
        length = _hear(out, heard, length, hop)
    if video is not None:
        report(0.5)
        moves = keyframe_motion(video)
        if len(moves) > 1:
            times, change = map(np.asarray, zip(*moves))
            length = length or int(times[-1] / hop) + 1
            filled = np.interp(np.arange(length) * hop, times, change)
            out.motion = np.round(rises(filled, hop, floor=1.0, start=2.0), 3).tolist()
    if chat is not None and chat.is_file():
        report(0.8)
        lines = chat_messages(chat)
        if lines:
            length = length or int(max(t for t, _ in lines) / hop) + 1
            index = (np.array([t for t, _ in lines]) / hop).astype(int)
            counts = np.bincount(index[(index >= 0) & (index < length)], minlength=length).astype(float)
            counts = np.convolve(counts, np.ones(max(1, int(5.0 / hop))), mode="same")
            base = np.maximum(rolling_median(counts, int(300 / hop)), 1.0)
            out.chat_rate = np.round(counts / base, 2).tolist()
            out.chat = np.round(np.clip((counts / base - 2.0) / 3.0, 0.0, 1.0), 3).tolist()
            out.chat_lines = lines
    report(1.0)
    return out


def cached(path: Path, make) -> Signals:
    signals, made = json_cache(path, make, Signals.from_json, Signals.to_json)
    if not made:
        return signals
    strongest = "; ".join(f"{p.time:.0f} s {'+'.join(p.kinds)}" for p in signals.peaks(5))
    log(f"[hook] reactions: {len(signals.peaks())} peaks" + (f", strongest: {strongest}" if strongest else ""))
    return signals
