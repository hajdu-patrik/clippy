"""Layer compilers (Strategy): each turns one aspect of the timeline into filter graph steps on the base render."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from ...captions import CaptionStyle, TextEvent, write_ass
from ...tools import probe_media
from ..assets import AssetIndex, AssetKind
from ..timeline import Bumper, TextLayer, TimeRange, Timeline
from .base import BaseRender
from .graph import Expr, Filter, FilterGraph, Pad

FRAME_W, FRAME_H = 1080, 1920
ROLE_SECONDS = 3.0
STEREO = Filter.of("aformat", sample_rates=48000, channel_layouts="stereo")


@dataclass
class CompileContext:
    graph: FilterGraph
    timeline: Timeline
    base: BaseRender
    assets: AssetIndex
    work: Path
    fonts_dir: str = "fonts"
    words: list[dict] = field(default_factory=list)
    hook: str | None = None

    @property
    def duration(self) -> float:
        return self.base.duration

    def window(self, span: TimeRange | None) -> tuple[float, float]:
        if span is None:
            return 0.0, self.duration
        return min(span.start, self.duration), min(span.end, self.duration)

    def text_window(self, layer: TextLayer) -> tuple[float, float]:
        if layer.span is not None:
            return self.window(layer.span)
        if layer.role == "hook":
            return 0.0, min(ROLE_SECONDS, self.duration)
        if layer.role == "cta":
            return max(0.0, self.duration - ROLE_SECONDS), self.duration
        return 0.0, self.duration


class VideoStage(ABC):
    @abstractmethod
    def apply(self, ctx: CompileContext, video: Pad) -> Pad: ...


class ColorStage(VideoStage):
    def apply(self, ctx: CompileContext, video: Pad) -> Pad:
        layer = ctx.timeline.layer("color")
        if layer is None:
            return video
        a, g = layer.adjust, ctx.graph
        filters = []
        if (a.brightness, a.contrast, a.saturation, a.gamma) != (0.0, 1.0, 1.0, 1.0):
            filters.append(Filter.of("eq", brightness=a.brightness, contrast=a.contrast, saturation=a.saturation,
                                     gamma=a.gamma))
        if a.temperature != 6500:
            filters.append(Filter.of("colortemperature", temperature=a.temperature))
        if a.vibrance:
            filters.append(Filter.of("vibrance", intensity=a.vibrance))
        video = g.chain(video, *filters)
        if layer.lut is None:
            return video
        lut = Filter.of("lut3d", file=ctx.assets.require(layer.lut, AssetKind.LUT).path.as_posix(), interp="tetrahedral")
        if layer.strength >= 0.999:
            return g.chain(video, lut)
        original, graded = g.split(video, 2)
        graded = g.chain(graded, lut)
        return g.chain([original, graded], Filter.of("blend", all_mode="normal", all_opacity=layer.strength))


class OverlayStage(VideoStage):
    def apply(self, ctx: CompileContext, video: Pad) -> Pad:
        g = ctx.graph
        for layer in ctx.timeline.layers_of("image"):
            asset = ctx.assets.require(layer.asset, AssetKind.IMAGE)
            start, end = ctx.window(layer.span)
            t = layer.transform
            source = g.input(asset.path, "-loop", "1", "-framerate", ctx.base.rate, "-t", f"{ctx.duration:.3f}")
            filters = [Filter.of("scale", round(t.width * FRAME_W / 2) * 2, -2), Filter.of("format", "rgba"),
                       Filter.of("colorchannelmixer", aa=t.opacity)]
            if layer.fade:
                filters += [Filter.of("fade", t="in", st=start, d=layer.fade, alpha=1),
                            Filter.of("fade", t="out", st=max(start, end - layer.fade), d=layer.fade, alpha=1)]
            image = g.chain(source.video, *filters)
            video = g.chain([video, image], Filter.of(
                "overlay", x=Expr(f"main_w*{t.x:.4f}-overlay_w/2"), y=Expr(f"main_h*{t.y:.4f}-overlay_h/2"),
                eof_action="pass", enable=Expr.between(start, end)))
        return video


class TextStage(VideoStage):
    """Captions, hook, CTA and free text in one ASS file, burnt in by libass."""

    def apply(self, ctx: CompileContext, video: Pad) -> Pad:
        captions = next(iter(ctx.timeline.layers_of("captions")), None)
        style = CaptionStyle()
        words: list[dict] = []
        if captions is not None and captions.visible:
            style = CaptionStyle(font=captions.font, size=captions.size, highlight=captions.highlight,
                                 uppercase=captions.uppercase, margin_v=round((1 - captions.position) * FRAME_H))
            words = ctx.words
        texts = tuple(self._event(ctx, layer) for layer in ctx.timeline.layers_of("text"))
        if not words and not texts:
            return video
        write_ass(ctx.work / "captions.ass", words, 0.0, ctx.duration, style, texts=texts)
        return ctx.graph.chain(video, Filter.of("subtitles", filename="captions.ass", fontsdir=ctx.fonts_dir))

    @staticmethod
    def _event(ctx: CompileContext, layer: TextLayer) -> TextEvent:
        start, end = ctx.text_window(layer)
        text = ctx.hook if layer.role == "hook" and ctx.hook else layer.text
        s, t = layer.style, layer.transform
        return TextEvent(text=text, start=start, end=end, x=t.x * FRAME_W, y=t.y * FRAME_H, size=s.size,
                         color=s.color, box=s.box, box_color=s.box_color, uppercase=s.uppercase, font=s.font,
                         width=t.width)


class AudioStage:
    """Speech from the base, music layers trimmed, faded, placed and ducked under the speech, summed."""

    def apply(self, ctx: CompileContext, speech: Pad) -> Pad:
        g, tl = ctx.graph, ctx.timeline
        speech = g.chain(speech, STEREO, *([Filter.of("volume", f"{tl.audio.speech_gain_db}dB")]
                                           if tl.audio.speech_gain_db else []))
        music = tl.layers_of("audio")
        if not music:
            return speech
        main, *keys = g.split(speech, len(music) + 1, audio=True)
        parts = [main]
        for layer, key in zip(music, keys):
            asset = ctx.assets.require(layer.asset, AssetKind.AUDIO, AssetKind.VIDEO)
            start, end = ctx.window(layer.span)
            length = max(0.1, end - start)
            track = g.chain(g.input(asset.path, "-stream_loop", "-1").audio,
                            Filter.of("atrim", duration=length), Filter.of("asetpts", "PTS-STARTPTS"), STEREO,
                            Filter.of("volume", f"{layer.gain_db}dB"),
                            Filter.of("afade", t="in", d=min(layer.fade_in, length / 2) or 0.01),
                            Filter.of("afade", t="out", st=max(0.0, length - layer.fade_out),
                                      d=min(layer.fade_out, length) or 0.01),
                            Filter.of("adelay", round(start * 1000), all=1))
            if layer.duck:
                track = g.chain([track, key], Filter.of("sidechaincompress", threshold=0.03, ratio=6, attack=20,
                                                        release=400, makeup=1))
            else:
                g.discard(key, audio=True)
            parts.append(track)
        return g.chain(parts, Filter.of("amix", inputs=len(parts), normalize=0, duration="first"))


@dataclass(frozen=True)
class Sequenced:
    video: Pad | None
    audio: Pad
    duration: float


class SequenceStage:
    """Intro and outro joined to the clip with xfade / acrossfade."""

    def apply(self, ctx: CompileContext, video: Pad | None, audio: Pad) -> Sequenced:
        g, total = ctx.graph, ctx.duration
        if video is not None and (ctx.timeline.intro or ctx.timeline.outro):
            video = g.chain(video, *self._normalise(ctx))
        for slot in ("intro", "outro"):
            bumper: Bumper | None = getattr(ctx.timeline, slot)
            if bumper is None:
                continue
            asset = ctx.assets.require(bumper.asset, AssetKind.VIDEO, AssetKind.IMAGE)
            length = bumper.duration or asset.duration or ROLE_SECONDS
            d = min(bumper.transition.duration, length / 2, total / 2)
            clip_v, clip_a = self._bumper(ctx, asset, length, with_video=video is not None)
            offset = (length - d) if slot == "intro" else (total - d)
            if video is not None:
                pair = [clip_v, video] if slot == "intro" else [video, clip_v]
                video = g.chain(pair, Filter.of("xfade", transition=bumper.transition.kind, duration=d, offset=offset))
            pair = [clip_a, audio] if slot == "intro" else [audio, clip_a]
            audio = g.chain(pair, Filter.of("acrossfade", d=d, c1="tri", c2="tri"))
            total = total + length - d
        return Sequenced(video, audio, total)

    @staticmethod
    def _normalise(ctx: CompileContext) -> list[Filter]:
        return [Filter.of("scale", FRAME_W, FRAME_H), Filter.of("setsar", 1), Filter.of("fps", ctx.base.rate),
                Filter.of("format", "yuv420p"), Filter.of("settb", "AVTB")]

    def _bumper(self, ctx: CompileContext, asset, length: float, with_video: bool) -> tuple[Pad | None, Pad]:
        g = ctx.graph
        if asset.kind == AssetKind.IMAGE:
            source = g.input(asset.path, "-loop", "1", "-framerate", ctx.base.rate, "-t", f"{length:.3f}")
            has_audio = False
        else:
            source = g.input(asset.path, "-t", f"{length:.3f}")
            has_audio = probe_media(asset.path)["audio"]
        video = None
        if with_video:
            video = g.chain(source.video, Filter.of("scale", FRAME_W, FRAME_H, force_original_aspect_ratio="increase"),
                            Filter.of("crop", FRAME_W, FRAME_H), *self._normalise(ctx)[1:])
        if has_audio:
            audio = g.chain(source.audio, Filter.of("atrim", duration=length), Filter.of("asetpts", "PTS-STARTPTS"),
                            STEREO)
        else:
            silence = g.input("anullsrc=r=48000:cl=stereo", "-f", "lavfi", "-t", f"{length:.3f}")
            audio = g.chain(silence.audio, STEREO)
        return video, audio
