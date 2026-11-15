"""Render one vertical 1080x1920 clip: one frame-accurate decode, Python compositing, one encode."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from .concurrency import RENDER
from . import audio as aud
from .analysis import ClipAnalysis, analyze, decode_cmd
from .captions import CaptionStyle, write_ass
from .compositing import OUT_H, OUT_W, CropCompositor, LayoutCompositor
from .director import guess_facecam, plan_frames
from .edit import EditPlan, build_edit
from .tools import (ROOT, ClippyError, atomic_path, ffmpeg_features, ffmpeg_path, fps_rational, log, probe_video,
                    read_exact, report, report_span)

LAYOUTS = ("auto", "face", "center", "blur", "stream")
SYSTEM_FONTS = ("ariblk.ttf", "impact.ttf", "arialbd.ttf", "arial.ttf")
VIDEO_TAGS = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv"]


INFLIGHT = 16
PIPE_BUFFER = 1 << 20
WORKERS = min(8, os.cpu_count() or 4)


@dataclass
class RenderSettings:
    layout: str = "auto"
    style: CaptionStyle | None = None
    hook: str = ""
    cta: str = ""
    facecam: tuple[int, int, int, int] | None = None
    tighten: bool = True
    punch: bool = True
    audio_mode: str = "voice"
    encoder: str = "auto"
    audio_src: Path | None = None
    translate: Callable[[list[dict], str], tuple[list[dict], str]] | None = None
    exclude: tuple[tuple[float, float], ...] = ()
    mezzanine: bool = False
    base_out: Path | None = None


@dataclass(frozen=True)
class Encoder:
    kind: str

    @classmethod
    def choose(cls, preference: str) -> "Encoder":
        if preference in ("nvenc", "x264"):
            return cls(preference)
        return cls("nvenc" if ffmpeg_features()["nvenc"] else "x264")

    def args(self, fps: float) -> list[str]:
        gop = str(int(round(fps * 2)))
        if self.kind == "nvenc":
            return ["-c:v", "h264_nvenc", "-preset", "p7", "-tune", "hq", "-rc", "vbr", "-cq", "17", "-b:v", "0",
                    "-maxrate", "40M", "-bufsize", "80M", "-spatial-aq", "1", "-temporal-aq", "1",
                    "-rc-lookahead", "32", "-bf", "3", "-profile:v", "high", "-g", gop]
        return ["-c:v", "libx264", "-preset", "slow", "-crf", "16", "-tune", "film", "-profile:v", "high", "-g", gop]

    def mezzanine_args(self, fps: float) -> list[str]:
        """Near-lossless intermediate for the editor's base render: re-encoded once more by the finishing pass."""
        gop = str(int(round(fps)))
        if self.kind == "nvenc":
            return ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "constqp", "-qp", "12",
                    "-profile:v", "high", "-bf", "0", "-g", gop]
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "10", "-profile:v", "high", "-g", gop]


def prepare_fonts(folder: Path) -> Path:
    """libass in static Windows builds has no font config, so it gets one folder with every usable font."""
    folder.mkdir(parents=True, exist_ok=True)
    sources = [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / n for n in SYSTEM_FONTS]
    user = ROOT / "fonts"
    if user.is_dir():
        sources += [p for p in user.iterdir() if p.suffix.lower() in (".ttf", ".otf")]
    for src in sources:
        if src.is_file() and not (folder / src.name).exists():
            with atomic_path(folder / src.name) as tmp:
                shutil.copy2(src, tmp)
    return folder


class ClipRenderer:
    def __init__(self, video: Path, words: list[dict], start: float, end: float, settings: RenderSettings):
        self.video, self.words, self.start, self.end = video, words, start, end
        self.span = end - start
        self.s = settings
        self.meta = probe_video(video)
        self.rate, self.fps = fps_rational(self.meta["fps"])

    def render(self, out_file: Path, work: Path, fonts: Path, analysis: ClipAnalysis | None = None) -> dict:
        edit = build_edit(self.words, self.start, self.end, self.fps,
                          env=aud.envelope(self.video, self.start, self.span), tighten=self.s.tighten,
                          exclude=self.s.exclude)
        with report_span(0.0, 0.3):
            an = analysis or analyze(self.video, self.start, self.span)
        plans, shots = plan_frames(an, self.s.layout, edit.join_frames, edit.sentence_frames, punch=self.s.punch)
        compositor = LayoutCompositor(self._facecam(an, shots))
        words, hook = self.s.translate(edit.words, self.s.hook) if self.s.translate else (edit.words, self.s.hook)
        if not self.s.mezzanine:
            write_ass(work / "captions.ass", words, 0.0, edit.duration, self.s.style or CaptionStyle(),
                      hook=hook, cta=self.s.cta)
        audio_src = self.s.audio_src or self.video
        measured = aud.measure(audio_src, self.start, self.span, edit.intervals, self.s.audio_mode)
        encoder = Encoder.choose(self.s.encoder)
        dw, dh = self._decode_size()
        upscale = OUT_H / dh * max((p.zoom for p in plans if p.mode == "crop"), default=1.0)
        v_graph = self._video_graph(work, fonts, upscale)
        a_graph = aud.final_graph(edit.intervals, self.s.audio_mode, measured, src="1:a")
        if self.s.base_out:
            v_graph = ("[0:v]split=2[vf][vb];" + self._video_graph(work, fonts, upscale, "[vf]") + ";"
                       + self._video_graph(work, fonts, upscale, "[vb]", "[vm]", captions=False))
            a_graph += ";[a]asplit=2[af][am]"
        cmd = self._encode_cmd(out_file, audio_src, encoder, v_graph, a_graph)
        with report_span(0.3, 1.0):
            written = self._pump(cmd, work, edit, plans, compositor, an.n_frames, dw, dh)
        if not self.s.mezzanine:
            write_cover(out_file, edit.duration)
        modes: dict[str, int] = {}
        for s, e, m in shots:
            modes[m] = modes.get(m, 0) + (e - s)
        return {
            "duration_out": round(edit.duration, 2), "dead_air_removed": edit.removed,
            "jump_cuts": len(edit.join_frames), "shots": len(shots), "layout_frames": modes,
            "face_detector": an.detector, "loudness_in": measured.get("input_i") if measured else None,
            "encoder": encoder.kind, "source": f"{self.meta['width']}x{self.meta['height']}@{self.fps:.2f}",
            "frames": written, "hook": hook,
            **(self._base_info(edit) if self.s.mezzanine else {}),
            **({"base": {**self._base_info(edit), "fps": self.fps, "rate": self.rate}} if self.s.base_out else {}),
        }

    def _base_info(self, edit: EditPlan) -> dict:
        return {"words": edit.words,
                "intervals": [(round(self.start + a, 4), round(self.start + b, 4)) for a, b in edit.intervals]}

    def _facecam(self, an: ClipAnalysis, shots) -> tuple[float, float, float, float] | None:
        if not any(m == "stream" for _, _, m in shots):
            return None
        if self.s.facecam:
            x, y, w, h = self.s.facecam
            W, H = self.meta["width"], self.meta["height"]
            return x / W, y / H, w / W, h / H
        cam = guess_facecam(an)
        if not cam:
            raise ClippyError("No facecam found for the stream layout. Pass --facecam x,y,w,h (source pixels)")
        return cam

    def _decode_size(self) -> tuple[int, int]:
        dh = min(self.meta["height"], 2160)
        dh -= dh % 2
        dw = int(round(self.meta["width"] * dh / self.meta["height"]))
        return dw - dw % 2, dh

    def _captions_filter(self, work: Path, fonts: Path) -> str:
        if ffmpeg_features()["subtitles"]:
            return f"subtitles=captions.ass:fontsdir={fonts_dir_for(work, fonts)},"
        log("[render] WARNING: this ffmpeg build cannot burn in captions (no libass).")
        return ""

    def _video_graph(self, work: Path, fonts: Path, upscale: float, src: str = "[0:v]", out: str = "[v]",
                     captions: bool = True) -> str:
        graph = src + ("unsharp=5:5:0.45:5:5:0.0," if upscale > 1.25 else "")
        if captions and not self.s.mezzanine:
            graph += self._captions_filter(work, fonts)
        return graph + ("scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,"
                        f"setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv{out}")

    def _encode_cmd(self, out_file: Path, audio_src: Path, encoder: Encoder, v_graph: str, a_graph: str) -> list[str]:
        return [
            ffmpeg_path(), "-hide_banner", "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{OUT_W}x{OUT_H}", "-framerate", self.rate, "-i", "-",
            "-ss", f"{self.start:.3f}", "-t", f"{self.span:.3f}", "-i", str(audio_src),
            "-filter_complex", f"{v_graph};{a_graph}", "-map", "[v]", "-map", "[af]" if self.s.base_out else "[a]",
            *(encoder.mezzanine_args(self.fps) if self.s.mezzanine else encoder.args(self.fps)),
            "-pix_fmt", "yuv420p", *VIDEO_TAGS,
            *(["-c:a", "flac", "-ar", "48000"] if self.s.mezzanine else
              ["-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-movflags", "+faststart"]),
            "-shortest", str(out_file),
            *(["-map", "[vm]", "-map", "[am]", *encoder.mezzanine_args(self.fps), "-pix_fmt", "yuv420p", *VIDEO_TAGS,
               "-c:a", "flac", "-ar", "48000", "-shortest", str(self.s.base_out)] if self.s.base_out else []),
        ]

    def _pump(self, cmd: list[str], work: Path, edit: EditPlan, plans, compositor: LayoutCompositor,
              n_frames: int, dw: int, dh: int) -> int:
        """Decode -> compose on a thread pool (OpenCV releases the GIL) -> encode, frames strictly in order."""
        keep = np.zeros(n_frames + int(self.fps * 2), dtype=bool)
        for ka, kb in edit.frames:
            keep[ka: min(kb, len(keep))] = True
        last_needed = max(kb for _, kb in edit.frames)
        used = [plans[min(j, len(plans) - 1)] for j in range(min(last_needed, n_frames, len(keep))) if keep[j]]
        strip = CropCompositor.columns(dw, dh, used)
        sw = strip[1] - strip[0] if strip else dw
        size = sw * dh * 3
        frames = [np.zeros((dh, dw, 3), np.uint8) for _ in range(INFLIGHT + WORKERS)]
        ring = [bytearray(size) for _ in frames] if strip else [f.data.cast("B") for f in frames]
        skipped = memoryview(bytearray(size))

        def compose(k: int, plan) -> np.ndarray:
            frame = frames[k]
            if strip:
                frame[:, strip[0]:strip[1]] = np.frombuffer(ring[k], np.uint8).reshape(dh, sw, 3)
            return np.ascontiguousarray(compositor.compose(frame, plan))

        log_path = work / "ffmpeg_encode.log"
        written = 0
        with open(log_path, "w", encoding="utf-8") as err:
            enc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=err, cwd=work)
            dec = subprocess.Popen(decode_cmd(self.video, self.start, self.span, dw, dh, self.rate,
                                              self.meta["matrix"], self.meta["range"], strip),
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=PIPE_BUFFER)
            inflight: deque = deque()
            submitted = 0
            try:
                with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                    for j in range(last_needed):
                        if j % 24 == 0:
                            report(j / last_needed)
                        if not keep[j]:
                            if read_exact(dec.stdout, skipped) < size:
                                break
                            continue
                        k = submitted % len(ring)
                        if read_exact(dec.stdout, memoryview(ring[k])) < size:
                            break
                        inflight.append(pool.submit(compose, k, plans[min(j, len(plans) - 1)]))
                        submitted += 1
                        if len(inflight) >= INFLIGHT:
                            enc.stdin.write(inflight.popleft().result())
                            written += 1
                    while inflight:
                        enc.stdin.write(inflight.popleft().result())
                        written += 1
            except BrokenPipeError:
                pass
            finally:
                dec.stdout.close()
                dec.kill()
                dec.wait()
                try:
                    enc.stdin.close()
                except BrokenPipeError:
                    pass
                enc.wait()
        if enc.returncode != 0:
            tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-15:])
            raise ClippyError(f"ffmpeg encode failed:\n{tail}")
        return written



def fonts_dir_for(work: Path, fonts: Path) -> str:
    """A relative path keeps ffmpeg's filter escaping trivial; across drives the fonts are copied next to the work."""
    try:
        return Path(os.path.relpath(fonts, work)).as_posix()
    except ValueError:
        shutil.copytree(fonts, work / "fonts", dirs_exist_ok=True)
        return "fonts"


def write_cover(out_file: Path, duration: float) -> None:
    """The cover image next to the video: a frame from the first seconds, with the hook already burnt in."""
    subprocess.run([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{min(1.0, duration / 3):.2f}",
                    "-i", str(out_file), "-frames:v", "1", "-q:v", "2", str(out_file.with_suffix(".jpg"))], check=False)


def render_clip(video: Path, words: list[dict], start: float, end: float, out_file: Path, work: Path, fonts: Path,
                settings: RenderSettings, analysis: ClipAnalysis | None = None) -> dict:
    with RENDER.hold():
        return ClipRenderer(video, words, start, end, settings).render(out_file, work, fonts, analysis)
