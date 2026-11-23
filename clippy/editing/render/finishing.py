"""The finishing pass: the cached base plus every layer of the timeline, in one FFmpeg run (two for final loudness)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ...audio import loudnorm_options, parse_loudnorm
from ...render import VIDEO_TAGS, Encoder, write_cover
from ...tools import ffmpeg_failed, ffmpeg_path
from ..assets import AssetIndex
from ..timeline import Timeline
from ..timeref import corrected
from .base import BaseRender
from .graph import Filter, FilterGraph, Pad
from .layers import (AudioStage, ColorStage, CompileContext, EffectStage, FormatStage, OverlayStage, SequenceStage,
                     SpeedStage, TextStage)

Translate = Callable[[list[dict], str], tuple[list[dict], str]]


@dataclass(frozen=True)
class RenderProfile:
    """`scale`: the output size as a share of the timeline's frame (1080x1920 for 9:16)."""

    name: str
    scale: float
    two_pass_loudness: bool
    audio_bitrate: str

    def size(self, frame: tuple[int, int]) -> tuple[int, int]:
        return tuple(max(2, round(side * self.scale / 2) * 2) for side in frame)

    def video_args(self, encoder: Encoder, fps: float) -> list[str]:
        if self.name == "final":
            return encoder.args(fps)
        if encoder.kind == "nvenc":
            return ["-c:v", "h264_nvenc", "-preset", "p1", "-rc", "vbr", "-cq", "30", "-b:v", "0"]
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28"]


FINAL = RenderProfile("final", 1.0, True, "256k")


class FinishingRenderer:
    VIDEO_STAGES = (FormatStage(), ColorStage(), OverlayStage(), EffectStage(), TextStage())

    def __init__(self, encoder: Encoder | None = None):
        self.encoder = encoder or Encoder.choose("auto")

    def render(self, timeline: Timeline, base: BaseRender, assets: AssetIndex, work: Path, fonts_dir: str,
               out_file: Path, profile: RenderProfile = FINAL, translate: Translate | None = None,
               source: Path | None = None, joined: dict[int, Path] | None = None) -> dict:
        """`source`: the original video (16:9 is built from it); `joined`: files of the clips joined after this one."""
        captions = next(iter(timeline.layers_of("captions")), None)
        words, hook = corrected(base.words, captions.fixes if captions else ()), None
        hook_layer = timeline.text_with_role("hook")
        if translate is not None:
            words, hook = translate(words, hook_layer.text if hook_layer else "")
        extra = {"source": source, "joined": dict(joined or {})}
        measured = self._measure(timeline, base, assets, work, extra) if profile.two_pass_loudness else None
        ctx = CompileContext(FilterGraph(), timeline, base, assets, work, fonts_dir, words, hook or None, **extra)
        video, audio, duration = self._compile(ctx, profile, measured, with_video=True)
        g = ctx.graph
        g.output(video)
        g.output(audio)
        cmd = [ffmpeg_path(), "-hide_banner", "-y", "-loglevel", "error", *g.input_args(),
               "-filter_complex", g.filter_complex(), *g.map_args(), *profile.video_args(self.encoder, base.fps),
               "-pix_fmt", "yuv420p", *VIDEO_TAGS, "-c:a", "aac", "-b:a", profile.audio_bitrate, "-ar", "48000",
               "-movflags", "+faststart", str(out_file)]
        self._run(cmd, work)
        write_cover(out_file, duration, words)
        return {"duration_out": round(duration, 2), "profile": profile.name, "hook": hook or "",
                "format": timeline.format,
                "loudness_in": measured.get("input_i") if measured else None}

    def _compile(self, ctx: CompileContext, profile: RenderProfile, measured: dict | None,
                 with_video: bool) -> tuple[Pad | None, Pad, float]:
        g = ctx.graph
        base = g.input(ctx.base.video)
        video = None
        if with_video:
            video = base.video
            for stage in self.VIDEO_STAGES:
                video = stage.apply(ctx, video)
        video, speech = SpeedStage().apply(ctx, video, base.audio)
        audio = AudioStage().apply(ctx, speech)
        joined = SequenceStage().apply(ctx, video, audio)
        audio = g.chain(joined.audio, Filter.of("loudnorm", **loudnorm_options(measured)),
                        Filter.of("aresample", 48000), prefix="a")
        if with_video:
            width, height = profile.size(ctx.size)
            video = g.chain(joined.video, Filter.of("scale", width, height, flags="lanczos"),
                            Filter.of("format", "yuv420p"),
                            Filter.of("setparams", color_primaries="bt709", color_trc="bt709", colorspace="bt709",
                                      range="tv"), prefix="v")
        return video, audio, joined.duration

    def _measure(self, timeline: Timeline, base: BaseRender, assets: AssetIndex, work: Path,
                 extra: dict) -> dict | None:
        ctx = CompileContext(FilterGraph(), timeline, base, assets, work, **extra)
        g = ctx.graph
        base_in = g.input(base.video)
        _, speech = SpeedStage().apply(ctx, None, base_in.audio)
        audio = AudioStage().apply(ctx, speech)
        joined = SequenceStage().apply(ctx, None, audio)
        g.output(g.chain(joined.audio, Filter.of("loudnorm", **loudnorm_options(None), print_format="json")))
        cmd = [ffmpeg_path(), "-hide_banner", "-nostats", *g.input_args(), "-filter_complex", g.filter_complex(),
               *g.map_args(), "-f", "null", "-"]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=work)
        return parse_loudnorm(proc.stderr if proc.returncode == 0 else "")

    @staticmethod
    def _run(cmd: list[str], work: Path) -> None:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=work)
        if proc.returncode != 0:
            raise ffmpeg_failed("finishing the edit", proc.stderr)
