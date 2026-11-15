"""The base render: cut + camera + processed speech, without any text or extra layer. It is the slow part (decode,
face analysis, virtual camera), so it is cached by the timeline's base key and reused by every finishing pass."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ...render import RenderSettings, prepare_fonts, render_clip
from ...tools import atomic_write_text, fps_rational, move_atomic, probe_video
from ..timeline import Timeline
from ..timeref import Clock


@dataclass(frozen=True)
class BaseRender:
    video: Path
    duration: float
    fps: float
    rate: str
    words: list[dict]
    intervals: tuple[tuple[float, float], ...]
    info: dict

    @property
    def clock(self) -> Clock:
        return Clock(self.intervals)

    def to_json(self) -> str:
        return json.dumps({"duration": self.duration, "fps": self.fps, "rate": self.rate, "words": self.words,
                           "intervals": self.intervals, "info": self.info}, ensure_ascii=False)

    @classmethod
    def load(cls, video: Path, meta: Path) -> "BaseRender":
        d = json.loads(meta.read_text(encoding="utf-8"))
        return cls(video, d["duration"], d["fps"], d["rate"], d["words"],
                   tuple(tuple(i) for i in d["intervals"]), d["info"])


class BaseStore:
    """data/chats/<id>/base/<key>.mkv + <key>.json; removed with the chat."""

    def __init__(self, folder: Path):
        self.folder = folder

    def paths(self, key: str) -> tuple[Path, Path]:
        return self.folder / f"{key}.mkv", self.folder / f"{key}.json"

    def get(self, key: str) -> BaseRender | None:
        video, meta = self.paths(key)
        return BaseRender.load(video, meta) if video.exists() and meta.exists() else None

    def put(self, key: str, render: BaseRender) -> BaseRender:
        video, meta = self.paths(key)
        self.folder.mkdir(parents=True, exist_ok=True)
        if render.video != video:
            move_atomic(render.video, video)
        atomic_write_text(meta, render.to_json())
        return BaseRender.load(video, meta)


class BaseRenderer:
    """Runs the existing clip renderer in mezzanine mode with the timeline's cut and camera."""

    def __init__(self, project):
        self.project = project

    def render(self, timeline: Timeline, work: Path) -> BaseRender:
        p = self.project.prepare()
        o = p.opts
        settings = RenderSettings(
            layout=timeline.camera.layout, punch=timeline.camera.punch, tighten=timeline.camera.tighten,
            audio_mode=timeline.audio.mode, encoder=o.encoder, facecam=o.facecam,
            audio_src=p.audio_source(timeline.audio.source_music_duck_db),
            exclude=tuple((r.start, r.end) for r in timeline.cut.removed), mezzanine=True)
        out = work / "base.mkv"
        src = timeline.cut.source
        info = render_clip(p.video, p.words, src.start, src.end, out, work, prepare_fonts(p.storage.cache("fonts")),
                           settings)
        rate, fps = fps_rational(probe_video(p.video)["fps"])
        words, intervals = info.pop("words"), tuple(tuple(i) for i in info.pop("intervals"))
        return BaseRender(out, float(info["duration_out"]), fps, rate, words, intervals, info)
