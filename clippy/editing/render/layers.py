"""Layer compilers (Strategy): each turns one aspect of the timeline into filter graph steps on the base render."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from ...captions import CaptionStyle, TextEvent, write_ass
from ...tools import probe_media
from ..assets import AssetIndex, AssetKind
from ..speed import SpeedMap, atempo_chain
from ..timeline import Bumper, StickerLayer, TextLayer, TimeRange, Timeline, Transition, VideoLayer
from . import stickers
from .base import BaseRender
from .graph import Expr, Filter, FilterGraph, Pad

ROLE_SECONDS = 3.0
STEREO = Filter.of("aformat", sample_rates=48000, channel_layouts="stereo")
BASE_W, BASE_H = 1080, 1920
SOUND_SECONDS = {"pop": 0.2, "ding": 1.2, "boom": 1.4, "click": 0.06, "whoosh": 0.7, "riser": 1.5, "bleep": 0.5}
SOUND_EXPRESSIONS = {
    "pop": "0.8*sin(2*PI*(500+1400*t)*t)*exp(-28*t)",
    "ding": "0.45*(sin(2*PI*1318*t)+0.5*sin(2*PI*2637*t))*exp(-4*t)",
    "boom": "0.9*sin(2*PI*(70-30*t)*t)*exp(-3.5*t)",
    "click": "0.8*sin(2*PI*2200*t)*exp(-90*t)",
    "whoosh": "0.7*(random(0)*2-1)*sin(PI*t*1.4286)",
    "riser": "0.35*sin(2*PI*(200+500*t*t)*t)*t*0.6667",
    "bleep": "0.45*sin(2*PI*1000*t)",
}


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
    source: Path | None = None
    joined: dict[int, Path] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.base.duration

    @property
    def size(self) -> tuple[int, int]:
        return self.timeline.size

    @cached_property
    def speed(self) -> SpeedMap:
        return SpeedMap.of(self.timeline.speed, self.duration)

    @property
    def out_duration(self) -> float:
        return self.speed.duration

    def window(self, span: TimeRange | None) -> tuple[float, float]:
        if span is None:
            return 0.0, self.duration
        return min(span.start, self.duration), min(span.end, self.duration)

    def heard(self, span: TimeRange | None) -> tuple[float, float]:
        """A window in the time the viewer sees after any speed change (for sound placed on the sped-up clip)."""
        start, end = self.window(span)
        return self.speed.out(start), self.speed.out(end)

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


class FormatStage(VideoStage):
    """9:16 is the base itself; 1:1 and 4:5 are cut from it, 16:9 is built from the source frames the base kept."""

    def apply(self, ctx: CompileContext, video: Pad) -> Pad:
        w, h = ctx.size
        if (w, h) == (BASE_W, BASE_H):
            return video
        if w < h or w == h:
            return ctx.graph.chain(video, Filter.of("crop", w, h, 0, round((BASE_H - h) * 0.4)))
        if ctx.source is None or not ctx.base.intervals:
            return ctx.graph.chain(video, *cover_frame(w, h))
        g = ctx.graph
        first = ctx.base.intervals[0][0]
        span = ctx.base.intervals[-1][1] - first + 0.5
        keep = "+".join(f"gte(t,{a - first:.3f})*lt(t,{b - first:.3f})" for a, b in ctx.base.intervals)
        source = g.input(ctx.source, "-ss", f"{first:.3f}", "-t", f"{span:.3f}")
        g.discard(video)
        return g.chain(source.video, Filter.of("select", Expr(keep)), Filter.of("setpts", Expr(f"N/({ctx.base.rate})/TB")),
                       Filter.of("scale", w, h, force_original_aspect_ratio="decrease"),
                       Filter.of("pad", w, h, Expr("(ow-iw)/2"), Expr("(oh-ih)/2")), Filter.of("setsar", 1),
                       Filter.of("format", "yuv420p"), Filter.of("trim", duration=ctx.duration))


def cover_frame(w: int, h: int) -> list[Filter]:
    return [Filter.of("scale", w, h, force_original_aspect_ratio="increase"), Filter.of("crop", w, h),
            Filter.of("setsar", 1)]


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
    """Images, B-roll videos and stickers, in layer order; each comes from its own input, so nothing is buffered."""

    def apply(self, ctx: CompileContext, video: Pad) -> Pad:
        for layer in ctx.timeline.layers:
            if layer.kind == "image":
                video = self._image(ctx, video, layer)
            elif layer.kind == "video":
                video = self._video(ctx, video, layer)
            elif layer.kind == "sticker":
                video = self._sticker(ctx, video, layer)
        return video

    @staticmethod
    def _image(ctx: CompileContext, video: Pad, layer) -> Pad:
        g, (w, _) = ctx.graph, ctx.size
        asset = ctx.assets.require(layer.asset, AssetKind.IMAGE)
        start, end = ctx.window(layer.span)
        t = layer.transform
        source = g.input(asset.path, "-loop", "1", "-framerate", ctx.base.rate, "-t", f"{ctx.duration:.3f}")
        filters = [Filter.of("scale", round(t.width * w / 2) * 2, -2), Filter.of("format", "rgba"),
                   Filter.of("colorchannelmixer", aa=t.opacity)]
        if layer.fade:
            filters += [Filter.of("fade", t="in", st=start, d=layer.fade, alpha=1),
                        Filter.of("fade", t="out", st=max(start, end - layer.fade), d=layer.fade, alpha=1)]
        image = g.chain(source.video, *filters)
        return g.chain([video, image], Filter.of(
            "overlay", x=Expr(f"main_w*{t.x:.4f}-overlay_w/2"), y=Expr(f"main_h*{t.y:.4f}-overlay_h/2"),
            eof_action="pass", enable=Expr.between(start, end)))

    @staticmethod
    def _video(ctx: CompileContext, video: Pad, layer: VideoLayer) -> Pad:
        g, (w, h) = ctx.graph, ctx.size
        asset = ctx.assets.require(layer.asset, AssetKind.VIDEO)
        start, end = ctx.window(layer.span)
        t = layer.transform
        source = g.input(asset.path, "-t", f"{end - start:.3f}")
        fit = cover_frame(w, h) if layer.full else [Filter.of("scale", round(t.width * w / 2) * 2, -2)]
        clip = g.chain(source.video, *fit, Filter.of("format", "yuv420p"),
                       Filter.of("setpts", Expr(f"PTS-STARTPTS+{start:.3f}/TB")))
        x, y = ("0", "0") if layer.full else (f"main_w*{t.x:.4f}-overlay_w/2", f"main_h*{t.y:.4f}-overlay_h/2")
        return g.chain([video, clip], Filter.of("overlay", x=Expr(x), y=Expr(y), eof_action="pass",
                                                enable=Expr.between(start, end)))

    @staticmethod
    def _sticker(ctx: CompileContext, video: Pad, layer: StickerLayer) -> Pad:
        g, (w, h) = ctx.graph, ctx.size
        start, end = ctx.window(layer.span)
        t = layer.transform
        png = stickers.draw(layer.emoji, round(t.width * w / 2) * 2, ctx.work / f"{layer.id}.png")
        source = g.input(png, "-loop", "1", "-framerate", ctx.base.rate, "-t", f"{ctx.duration:.3f}")
        filters = [Filter.of("format", "rgba")]
        if layer.motion != "none":
            fade = 0.12 if layer.motion == "pop" else 0.25
            filters += [Filter.of("fade", t="in", st=start, d=fade, alpha=1),
                        Filter.of("fade", t="out", st=max(start, end - 0.2), d=0.2, alpha=1)]
        sticker = g.chain(source.video, *filters)
        x, y = f"main_w*{t.x:.4f}-overlay_w/2", f"main_h*{t.y:.4f}-overlay_h/2"
        local, amp = f"(t-{start:.3f})", round(0.025 * h)
        if layer.motion == "pop":
            y += f"-{amp * 2}*exp(-7*{local})*abs(sin(12*{local}))"
        elif layer.motion == "bounce":
            y += f"-{amp}*abs(sin(5*{local}))"
        elif layer.motion == "float":
            x += f"+{amp // 2}*cos(1.7*{local})"
            y += f"+{amp}*sin(2.2*{local})"
        return g.chain([video, sticker], Filter.of("overlay", x=Expr(x), y=Expr(y), eof_action="pass",
                                                   enable=Expr.between(start, end)))


class EffectStage(VideoStage):
    """Effects as timeline-enabled filters (no split, no buffering). Zoom, slow zoom and shake share one scale and
    crop evaluated per frame (zoompan buffers the whole clip in front of a transition and runs out of memory)."""

    def apply(self, ctx: CompileContext, video: Pad) -> Pad:
        effects = ctx.timeline.layers_of("effect")
        if not effects:
            return video
        g, (w, h) = ctx.graph, ctx.size
        zoom, dx, dy, filters = ["1"], ["0"], ["0"], []
        for layer in effects:
            s, e = ctx.window(layer.span)
            on, k = f"between(t,{s:.3f},{e:.3f})", layer.strength
            if layer.effect == "zoom":
                zoom.append(f"{0.35 * k:.3f}*{on}")
            elif layer.effect == "slow_zoom":
                zoom.append(f"{0.3 * k:.3f}*clip((t-{s:.3f})/{max(0.1, e - s):.3f},0,1)*{on}")
            elif layer.effect == "shake":
                zoom.append(f"{0.08 * k:.3f}*{on}")
                dx.append(f"{0.025 * w * k:.1f}*sin(t*47)*{on}")
                dy.append(f"{0.02 * h * k:.1f}*cos(t*53)*{on}")
            elif layer.effect in ("flash", "fade_black"):
                d = max(0.05, e - s)
                curve = (f"{k:.3f}*(1-(t-{s:.3f})/{d:.3f})" if layer.effect == "flash"
                         else f"-{k:.3f}*(1-abs(2*(t-{s:.3f})/{d:.3f}-1))")
                filters.append(Filter.of("eq", brightness=Expr(curve), eval="frame", enable=Expr.between(s, e)))
            elif layer.effect == "black_white":
                filters.append(Filter.of("hue", s=round(1 - k, 3), enable=Expr.between(s, e)))
            elif layer.effect == "blur":
                filters.append(Filter.of("gblur", sigma=round(4 + 26 * k, 1), enable=Expr.between(s, e)))
            elif layer.effect == "vignette":
                filters.append(Filter.of("vignette", angle=round(0.3 + 0.6 * k, 3), enable=Expr.between(s, e)))
        if len(zoom) > 1:
            z = "+".join(zoom)
            filters[:0] = [
                Filter.of("scale", w=Expr(f"2*trunc(iw*({z})/2)"), h=Expr(f"2*trunc(ih*({z})/2)"), eval="frame"),
                Filter.of("crop", w, h, x=Expr(f"(in_w-{w})/2+({'+'.join(dx)})"),
                          y=Expr(f"(in_h-{h})/2+({'+'.join(dy)})")),
            ]
        return g.chain(video, *filters)


class TextStage(VideoStage):
    """Captions, opening text, CTA and free text in one ASS file, burnt in by libass."""

    def apply(self, ctx: CompileContext, video: Pad) -> Pad:
        w, h = ctx.size
        captions = next(iter(ctx.timeline.layers_of("captions")), None)
        style = CaptionStyle(max_chars=28 if w > h else 18)
        words: list[dict] = []
        if captions is not None and captions.visible:
            style = CaptionStyle(font=captions.font, size=captions.size, highlight=captions.highlight,
                                 uppercase=captions.uppercase, margin_v=round((1 - captions.position) * h),
                                 max_chars=28 if w > h else 18)
            words = ctx.words
        texts = tuple(self._event(ctx, layer) for layer in ctx.timeline.layers_of("text"))
        if not words and not texts:
            return video
        write_ass(ctx.work / "captions.ass", words, 0.0, ctx.duration, style, texts=texts, frame=(w, h))
        return ctx.graph.chain(video, Filter.of("subtitles", filename="captions.ass", fontsdir=ctx.fonts_dir))

    @staticmethod
    def _event(ctx: CompileContext, layer: TextLayer) -> TextEvent:
        w, h = ctx.size
        start, end = ctx.text_window(layer)
        text = ctx.hook if layer.role == "hook" and ctx.hook else layer.text
        s, t = layer.style, layer.transform
        return TextEvent(text=text, start=start, end=end, x=t.x * w, y=t.y * h, size=s.size,
                         color=s.color, box=s.box, box_color=s.box_color, uppercase=s.uppercase, font=s.font,
                         width=t.width, animation=s.animation)


class SpeedStage:
    """Each part of the clip played at its speed: setpts for the picture, atempo (pitch kept) for the sound."""

    def apply(self, ctx: CompileContext, video: Pad | None, audio: Pad) -> tuple[Pad | None, Pad]:
        if ctx.speed.identity:
            return video, audio
        g, segments = ctx.graph, ctx.speed.segments
        n = len(segments)
        videos = (g.split(video, n) if n > 1 else [video]) if video is not None else [None] * n
        audios = g.split(audio, n, audio=True) if n > 1 else [audio]
        v_parts, a_parts = [], []
        for (a, b, f), v, au in zip(segments, videos, audios):
            if video is not None:
                v_parts.append(g.chain(v, Filter.of("trim", start=a, end=b),
                                       Filter.of("setpts", Expr(f"(PTS-STARTPTS)/{f:.6f}"))))
            a_parts.append(g.chain(au, Filter.of("atrim", start=a, end=b), Filter.of("asetpts", "PTS-STARTPTS"),
                                   *(Filter.of("atempo", round(x, 6)) for x in atempo_chain(f))))
        if video is not None:
            video = g.chain(v_parts, Filter.of("concat", n=len(v_parts), v=1, a=0), Filter.of("fps", ctx.base.rate))
        audio = g.chain(a_parts, Filter.of("concat", n=len(a_parts), v=0, a=1))
        return video, audio


class AudioStage:
    """Speech, then music (trimmed, faded, ducked under the speech), sound effects and B-roll sound, summed. It runs
    after the speed change, so every window is placed in the time the viewer hears."""

    def apply(self, ctx: CompileContext, speech: Pad) -> Pad:
        g, tl = ctx.graph, ctx.timeline
        speech = g.chain(speech, STEREO, *([Filter.of("volume", f"{tl.audio.speech_gain_db}dB")]
                                           if tl.audio.speech_gain_db else []))
        music = tl.layers_of("audio")
        extras = [self._sound(ctx, layer) for layer in tl.layers_of("sound")]
        extras += [self._broll(ctx, layer) for layer in tl.layers_of("video") if layer.gain_db is not None]
        extras = [x for x in extras if x is not None]
        if not music and not extras:
            return speech
        parts = [speech]
        if music:
            main, *keys = g.split(speech, len(music) + 1, audio=True)
            parts = [main]
            for layer, key in zip(music, keys):
                parts.append(self._music(ctx, layer, key))
        parts += extras
        return g.chain(parts, Filter.of("amix", inputs=len(parts), normalize=0, duration="first"))

    @staticmethod
    def _music(ctx: CompileContext, layer, key: Pad) -> Pad:
        g = ctx.graph
        asset = ctx.assets.require(layer.asset, AssetKind.AUDIO, AssetKind.VIDEO)
        start, end = ctx.heard(layer.span) if layer.span else (0.0, ctx.out_duration)
        length = max(0.1, end - start)
        track = g.chain(g.input(asset.path, "-stream_loop", "-1").audio,
                        Filter.of("atrim", duration=length), Filter.of("asetpts", "PTS-STARTPTS"), STEREO,
                        Filter.of("volume", f"{layer.gain_db}dB"),
                        Filter.of("afade", t="in", d=min(layer.fade_in, length / 2) or 0.01),
                        Filter.of("afade", t="out", st=max(0.0, length - layer.fade_out),
                                  d=min(layer.fade_out, length) or 0.01),
                        Filter.of("adelay", round(start * 1000), all=1))
        if layer.duck:
            return g.chain([track, key], Filter.of("sidechaincompress", threshold=0.03, ratio=6, attack=20,
                                                   release=400, makeup=1))
        g.discard(key, audio=True)
        return track

    @staticmethod
    def _sound(ctx: CompileContext, layer) -> Pad:
        g = ctx.graph
        at = ctx.speed.out(min(layer.at, ctx.duration))
        if layer.sound is not None:
            seconds = SOUND_SECONDS[layer.sound]
            source = g.input(f"aevalsrc='{SOUND_EXPRESSIONS[layer.sound]}':s=48000:d={seconds}", "-f", "lavfi")
            shape = [Filter.of("highpass", f=900), Filter.of("lowpass", f=5000)] if layer.sound == "whoosh" else []
        else:
            asset = ctx.assets.require(layer.asset, AssetKind.AUDIO, AssetKind.VIDEO)
            source, shape = g.input(asset.path, "-t", "10"), []
        return g.chain(source.audio, *shape, Filter.of("asetpts", "PTS-STARTPTS"), STEREO,
                       Filter.of("volume", f"{layer.gain_db}dB"), Filter.of("adelay", round(at * 1000), all=1))

    @staticmethod
    def _broll(ctx: CompileContext, layer: VideoLayer) -> Pad | None:
        asset = ctx.assets.require(layer.asset, AssetKind.VIDEO)
        if not probe_media(asset.path)["audio"]:
            return None
        start, end = ctx.heard(layer.span) if layer.span else (0.0, ctx.out_duration)
        g = ctx.graph
        return g.chain(g.input(asset.path, "-t", f"{max(0.1, end - start):.3f}").audio,
                       Filter.of("asetpts", "PTS-STARTPTS"), STEREO, Filter.of("volume", f"{layer.gain_db}dB"),
                       Filter.of("adelay", round(start * 1000), all=1))


@dataclass(frozen=True)
class Sequenced:
    video: Pad | None
    audio: Pad
    duration: float


@dataclass(frozen=True)
class Segment:
    """Something played before or after the clip: an intro / outro attachment or another clip of the chat."""

    path: Path
    still: bool
    length: float
    transition: Transition


class SequenceStage:
    """Intro, the clip, joined clips and outro, one after the other with xfade / acrossfade."""

    def apply(self, ctx: CompileContext, video: Pad | None, audio: Pad) -> Sequenced:
        g, total = ctx.graph, ctx.out_duration
        before, after = self._segments(ctx)
        if video is not None and (before or after):
            video = g.chain(video, *self._normalise(ctx))
        for slot, segment in [("before", s) for s in before] + [("after", s) for s in after]:
            d = min(segment.transition.duration, segment.length / 2, total / 2)
            clip_v, clip_a = self._clip(ctx, segment, with_video=video is not None)
            offset = (segment.length - d) if slot == "before" else (total - d)
            if video is not None:
                pair = [clip_v, video] if slot == "before" else [video, clip_v]
                video = g.chain(pair, Filter.of("xfade", transition=segment.transition.kind, duration=d,
                                                offset=offset))
            pair = [clip_a, audio] if slot == "before" else [audio, clip_a]
            audio = g.chain(pair, Filter.of("acrossfade", d=d, c1="tri", c2="tri"))
            total = total + segment.length - d
        return Sequenced(video, audio, total)

    @staticmethod
    def _segments(ctx: CompileContext) -> tuple[list[Segment], list[Segment]]:
        tl = ctx.timeline

        def bumper(b: Bumper | None) -> list[Segment]:
            if b is None:
                return []
            asset = ctx.assets.require(b.asset, AssetKind.VIDEO, AssetKind.IMAGE)
            return [Segment(asset.path, asset.kind == AssetKind.IMAGE, b.duration or asset.duration or ROLE_SECONDS,
                            b.transition)]

        joins = [Segment(ctx.joined[j.clip], False, probe_media(ctx.joined[j.clip])["duration"], j.transition)
                 for j in tl.joins if j.clip in ctx.joined]
        return bumper(tl.intro), joins + bumper(tl.outro)

    @staticmethod
    def _normalise(ctx: CompileContext) -> list[Filter]:
        w, h = ctx.size
        return [Filter.of("scale", w, h), Filter.of("setsar", 1), Filter.of("fps", ctx.base.rate),
                Filter.of("format", "yuv420p"), Filter.of("settb", "AVTB")]

    def _clip(self, ctx: CompileContext, segment: Segment, with_video: bool) -> tuple[Pad | None, Pad]:
        g, (w, h), length = ctx.graph, ctx.size, segment.length
        if segment.still:
            source = g.input(segment.path, "-loop", "1", "-framerate", ctx.base.rate, "-t", f"{length:.3f}")
            has_audio = False
        else:
            source = g.input(segment.path, "-t", f"{length:.3f}")
            has_audio = probe_media(segment.path)["audio"]
        video = None
        if with_video:
            video = g.chain(source.video, *cover_frame(w, h), *self._normalise(ctx)[2:])
        if has_audio:
            audio = g.chain(source.audio, Filter.of("atrim", duration=length), Filter.of("asetpts", "PTS-STARTPTS"),
                            STEREO)
        else:
            silence = g.input("anullsrc=r=48000:cl=stereo", "-f", "lavfi", "-t", f"{length:.3f}")
            audio = g.chain(silence.audio, STEREO)
        return video, audio
