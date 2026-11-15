"""Options + Project: prepare (download, voice isolation, transcript, alignment) -> select -> render.
Used by the CLI and the web app; every stage is cached in Storage."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from urllib.parse import urlsplit

from . import align, covers, i18n, separation, signals, speakers
from .analysis import ClipAnalysis, analyze
from .captions import CaptionStyle
from .concurrency import GPU, SOURCE, GuardedLLM
from .download import Remote, cached_video, fetch, fetch_audio, fetch_chat, fetch_video, is_url, probe
from .highlights import Clip, PickCache, find_highlights
from .settings import URL, Settings, site_of
from .llm import create_llm
from .render import RenderSettings, prepare_fonts, render_clip
from .storage import Storage
from .tools import (ClippyError, ThreadContext, atomic_write_text, fmt_ts, log, move_atomic, probe_media,
                    read_json, report_span, write_json)
from .translate import CaptionTranslator
from .transcribe import all_words, transcribe


def require_sound(media: Path, source: str) -> None:
    """Clips are picked and captioned by what is said, so a video without any sound track stops here, clearly."""
    if not probe_media(media).get("audio"):
        raise ClippyError(key="error.no_sound", host=urlsplit(source).hostname or Path(source).name)


SHORT_SOURCE_MARGIN = 10.0
SHORT_SOURCE_FACTOR = 1.5
CHAT_WAIT = 180.0
SPARSE_SPEECH = 0.35


@dataclass
class Options:
    n: int = 5
    min_len: float = 20
    max_len: float = 60
    instructions: str = ""
    llm: str | None = None
    model: str | None = None
    lang: str | None = None
    captions: str | None = None
    whisper: str = "auto"
    vocab: str = ""
    title_vocab: bool = True
    device: str = "auto"
    isolate: str = "auto"
    align: bool = True
    layout: str = "auto"
    facecam: tuple[int, int, int, int] | None = None
    cta: str = ""
    hook: bool = False
    highlight: str = "yellow"
    uppercase: bool = True
    font: str = "Arial Black"
    tighten: bool = True
    punch: bool = False
    audio: str = "voice"
    duck: float = 0.0
    encoder: str = "auto"
    cookies_from_browser: str | None = field(default_factory=lambda: os.environ.get("CLIPPY_COOKIES_FROM_BROWSER") or None)

    def merged(self, overrides: dict | None) -> "Options":
        names = {f.name for f in fields(self)}
        return replace(self, **{k: v for k, v in (overrides or {}).items() if k in names})

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Options":
        names = {f.name for f in fields(cls)}
        d = {k: v for k, v in (d or {}).items() if k in names}
        if d.get("facecam"):
            d["facecam"] = tuple(d["facecam"])
        return cls(**d)

    def render_settings(self, hook: str, audio_src: Path | None, translate=None) -> RenderSettings:
        return RenderSettings(translate=translate,
            layout=self.layout, style=CaptionStyle(font=self.font, highlight=self.highlight, uppercase=self.uppercase),
            hook=hook if self.hook else "", cta=self.cta, facecam=self.facecam, tighten=self.tighten,
            punch=self.punch, audio_mode=self.audio, encoder=self.encoder, audio_src=audio_src)


LINK_LINES = {"YouTube": "post.youtube", "Instagram": "post.instagram", "TikTok": "post.tiktok"}


def link_lines(links, topic: str, tr) -> list[str]:
    """The creator's own profiles, one line each: a follow line for YouTube, the platform's name for the others."""
    out = []
    for line in links:
        found = URL.search(line)
        if not found:
            continue
        url, site = found.group(), site_of(found.group())
        if site == "YouTube":
            out.append(tr("post.subscribe", topic=topic, url=url) if topic else tr("post.subscribe_plain", url=url))
        else:
            label = line[:found.start()].strip(" :") or site
            out.append(tr(LINK_LINES.get(site, "post.link"), label=label, url=url))
    return out


def post_text(clip: Clip, source: str, info: dict, links: list[str] | tuple = ()) -> str:
    """Ready to paste: the post caption (the teaser, the comment question, the original video, the creator's own
    profiles, hashtags), then the title, the pinned comment and the notes."""
    lang = i18n.detect(f"{clip.title} {clip.description} {clip.hook}")

    def tr(key: str, **params) -> str:
        return i18n.t(lang, key, **params)

    question = f"{clip.question} {tr('post.comments')}" if clip.question else ""
    blocks = (clip.description or clip.title, question, tr("post.original", url=source) if source else "",
              "\n".join(link_lines(links, clip.topic, tr)), " ".join(clip.hashtags))
    caption = "\n\n".join(x for x in blocks if x)
    kind = f" ({tr('hooktype.' + clip.hook_type)})" if clip.hook_type else ""
    notes = [f"{tr('post.title')}: {clip.title}"]
    if clip.alt_title:
        notes.append(f"{tr('post.alt_title')}: {clip.alt_title}")
    notes.append(f"{tr('post.pinned')}: {tr('post.full_video', url=source)}")
    if clip.hook:
        notes.append(f"{tr('post.hook')}{kind}: {clip.hook}")
    if clip.alt_hook:
        notes.append(f"{tr('post.alt_hook')}: {clip.alt_hook}")
    notes += [f"{tr('post.source')}: {source}  ({fmt_ts(clip.start)}-{fmt_ts(clip.end)})",
              tr("post.length", s=f"{info['duration_out']:.1f}", removed=f"{info['dead_air_removed']:.1f}"),
              f"{tr('post.score')}: {clip.score:.0f}", f"{tr('post.why')}: {clip.reason}"]
    if clip.risks:
        notes.append(f"{tr('post.check')}: " + "; ".join(tr("risk." + r) for r in clip.risks))
    notes.append(tr("post.credit"))
    return f"{caption}\n\n---\n" + "\n".join(notes) + "\n"


class Project:
    def __init__(self, source: str, opts: Options | None = None, storage: Storage | None = None,
                 scratch_root: Path | None = None):
        self.source = source
        self.opts = opts or Options()
        self.storage = storage or Storage()
        self.scratch_root = scratch_root
        self._video: Path | None = None
        self._video_task: Future | None = None
        self._downloader: ThreadPoolExecutor | None = None
        self._downloaded = 0.0
        self._stem: str | None = None
        self.sound: Path | None = None
        self._chat_task: Future | None = None
        self._side: ThreadPoolExecutor | None = None
        self.source_duration: float | None = None
        self.is_stream = False
        self.title = ""
        self.transcript: dict | None = None
        self.words: list[dict] = []
        self.stems: dict | None = None
        self._analyses: dict[tuple[float, float], Future] = {}
        self._analyzer: ThreadPoolExecutor | None = None
        self.llm_desc = ""

    @property
    def video(self) -> Path | None:
        """The source video. A new one downloads in the background; the first reader that needs it waits for it."""
        task = self._video_task
        if task is not None:
            while not wait([task], timeout=3.0).done:
                log(f"[render] waiting for the video download ({round(100 * self._downloaded)}%)…")
            self._video, self._video_task = task.result(), None
        return self._video

    @video.setter
    def video(self, path: Path | None) -> None:
        self._video, self._video_task = path, None

    @property
    def video_stem(self) -> str | None:
        return self._stem or (self._video.stem if self._video else None)

    def prepare(self) -> "Project":
        """Transcript and stems. For a new video only the sound is fetched first: voice isolation, the transcript
        and clip picking run while the (much larger) video keeps downloading."""
        if self.transcript is not None:
            return self
        o, st = self.opts, self.storage
        downloads = st.cache("downloads")
        sound = None
        if not is_url(self.source):
            self.video, self.title = fetch(self.source, downloads)
            key = self.video
        else:
            remote = probe(self.source, downloads, cookies_from_browser=o.cookies_from_browser)
            self.title, self.source_duration = remote.title, remote.duration
            self.is_stream = remote.is_stream
            if remote.has_chat:
                self._side = self._side or ThreadPoolExecutor(max_workers=1, thread_name_prefix="clippy-chat")
                self._chat_task = self._side.submit(fetch_chat, remote, downloads, o.cookies_from_browser)
            key = cached_video(downloads, remote.vid)
            if key is not None:
                log(f"[download] already here: {key.name}")
                self.video = key
            else:
                self._download_in_background(remote)
                key = downloads / f"{remote.vid}.mkv"
                if remote.sound_track:
                    try:
                        sound = fetch_audio(remote, downloads, o.cookies_from_browser)
                    except Exception as exc:
                        log(f"[download] the sound alone failed ({type(exc).__name__}); waiting for the video")
                        key = self.video
                else:
                    key = self.video
        self._stem = key.stem
        self.sound = sound or key
        require_sound(sound or key, self.source)
        with SOURCE.hold(key.stem):
            stems = None
            if o.isolate == "on" or (o.isolate == "auto" and separation.available()):
                if not separation.available():
                    raise ClippyError("Voice isolation needs the studio extras: pip install -r requirements-ml.txt")
                stems = separation.separate(key, st.cache("audio"), gpu=self._gpu, sound=sound)
            vocab = ", ".join(v for v in (o.vocab, self.title if o.title_vocab else "") if v)
            voice = stems["vocals"] if stems else None
            tr = transcribe(key, st.cache("transcripts"), model_name=o.whisper, language=o.lang,
                            device=o.device, vocab=vocab, audio_source=voice, gpu=self._gpu, sound=sound)
            if o.align and align.available():
                digest = hashlib.sha1(json.dumps(tr, ensure_ascii=False).encode("utf-8")).hexdigest()[:10]
                tr = align.align(tr, voice or sound or key,
                                 st.cache("transcripts") / f"{key.stem}.{digest}.aligned.json", gpu=self._gpu)
            elif o.align:
                tr = align.refine_onsets(tr, voice or sound or key)
        self.stems, self.transcript, self.words = stems, tr, all_words(tr)
        return self

    def _download_in_background(self, remote: Remote) -> None:
        if self._downloader is None:
            self._downloader = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clippy-download")
        self._downloaded = 0.0
        track = ThreadContext(reporter=lambda f: setattr(self, "_downloaded", f))
        self._video, self._video_task = None, self._downloader.submit(
            track.wrap(fetch_video), remote, self.storage.cache("downloads"),
            cookies_from_browser=self.opts.cookies_from_browser)

    @contextmanager
    def _gpu(self):
        """GPU work (Demucs, Whisper, alignment) runs alone on the GPU, with the LLM moved out of VRAM first."""
        with GPU.hold():
            create_llm(self.opts.llm, self.opts.model).unload()
            yield

    def select(self, n: int | None = None, exclude: list[Clip] | tuple = (), instructions: str | None = None,
               opts: Options | None = None, lessons: str = "") -> list[Clip]:
        """The best moments. A short source (a TikTok, a Reel) becomes one clip of all of it, the LLM only writes its
        texts; without speech it gets no texts, since moments and hooks come from what is said. A short video's own
        first seconds are its hook, so it gets no opening text over them."""
        self.prepare()
        o = opts or self.opts
        duration = float((self.transcript or {}).get("duration") or (self.words[-1]["end"] if self.words else 0.0))
        if self.source_duration and (not duration or duration > self.source_duration + 1.0):
            duration = self.source_duration
        min_len, max_len, n = o.min_len, o.max_len, n or o.n
        if not self.words and duration and duration <= SHORT_SOURCE_FACTOR * max_len:
            if exclude:
                raise ClippyError(key="error.empty_transcript")
            log("[hook] no speech: the whole video becomes one clip")
            return [Clip(0.0, min(duration, max_len), 50.0, title=self.title[:70], reason="no speech in the video")]
        if not self.words:
            log("[hook] no speech: the moments come from the reactions (sound, picture, chat)")
        short = bool(duration) and duration < min_len + SHORT_SOURCE_MARGIN
        if short and exclude:
            raise ClippyError(key="error.no_suitable_clip")
        if short:
            min_len, max_len, n = min(min_len, max(3.0, 0.6 * duration)), min(max_len, duration), 1
            log(f"[hook] short video ({duration:.0f} s): one clip of all of it")
        with report_span(0.0, 0.05):
            reactions = None if short else self.reactions(duration)
        llm = GuardedLLM(create_llm(o.llm, o.model))
        self.llm_desc = llm.describe()
        clips = find_highlights(self.transcript, llm, max_clips=n, min_len=min_len, max_len=max_len,
                                title=self.title, exclude=list(exclude), text_language=o.captions,
                                instructions=o.instructions if instructions is None else instructions,
                                cache=PickCache(self.storage.cache("picks"), self.video_stem) if self.video_stem else None,
                                signals=reactions, lessons=lessons, speakers=self.speaker_turns)
        if not clips:
            raise ClippyError(key="error.no_suitable_clip")
        if short:
            clips[0].start, clips[0].end = 0.0, min(duration, o.max_len)
            clips[0].hook = clips[0].alt_hook = ""
        return clips[:1] if short else clips

    def reactions(self, duration: float) -> signals.Signals | None:
        """The video's reactions: its sound always, the stream chat when the site keeps it, the picture when the video
        is here already or when little is said (gameplay, a stream without commentary), so it is worth waiting for."""
        stem = self.video_stem
        if not stem:
            return None
        try:
            chat = self._chat_ready()
            speech = sum(w["end"] - w["start"] for w in self.words)
            sparse = bool(duration) and speech < SPARSE_SPEECH * duration
            task = self._video_task
            if task is not None and (task.done() or sparse):
                while not wait([task], timeout=5.0).done:
                    log(f"[hook] waiting for the video to read the action on screen ({round(100 * self._downloaded)}%)")
            video = self.video if self._video_task is None or self._video_task.done() else None
            name = f"{stem}.{'v' if video else ''}{'c' if chat else ''}.json"
            sound = self.sound if self.sound and self.sound.is_file() else video
            return signals.cached(self.storage.cache("signals") / name,
                                  lambda: signals.build(sound, self.words, video, chat))
        except Exception as exc:
            log(f"[hook] reactions skipped ({type(exc).__name__}: {str(exc)[:120]})")
            return None

    def speaker_turns(self, sentences: list[dict]) -> list[str]:
        """Who speaks each sentence, from the clean voice when it was separated; nothing when it cannot tell."""
        voice = Path(self.stems["vocals"]) if self.stems and self.stems.get("vocals") else self.sound
        if not self.video_stem or voice is None or not Path(voice).is_file():
            return []
        try:
            path = self.storage.cache("signals") / f"{self.video_stem}.speakers.{len(sentences)}.json"
            turns = speakers.cached(path, lambda: speakers.speaker_turns(Path(voice), sentences))
            return turns if len(turns) == len(sentences) else []
        except Exception as exc:
            log(f"[hook] voices skipped ({type(exc).__name__}: {str(exc)[:120]})")
            return []

    def _chat_ready(self) -> Path | None:
        task = self._chat_task
        if task is None:
            return None
        if not wait([task], timeout=CHAT_WAIT).done:
            log("[hook] the chat replay is still downloading: picking without it")
            return None
        try:
            return task.result()
        except Exception as exc:
            log(f"[hook] chat replay skipped ({type(exc).__name__})")
            return None

    def audio_source(self, duck: float) -> Path | None:
        if duck <= 0:
            return None
        if not self.stems:
            raise ClippyError("Music ducking needs voice/music separation (requirements-ml.txt).")
        with SOURCE.hold(self.video.stem):
            return separation.ducked_mix(self.stems, self.storage.cache("audio"), self.video, duck)

    @property
    def spoken_language(self) -> str | None:
        return (self.transcript or {}).get("language")

    def caption_translator(self, target: str | None, o: Options | None = None) -> CaptionTranslator | None:
        """Captions in another language than the spoken one need the LLM; without one they stay as spoken."""
        o = o or self.opts
        if not target or target == self.spoken_language:
            return None
        llm = create_llm(o.llm, o.model)
        if llm.backend == "none":
            log("[render] caption translation needs an LLM backend; captions stay in the spoken language")
            return None
        return CaptionTranslator(GuardedLLM(llm), target)

    def prefetch(self, clip: Clip) -> None:
        """Start the face/shot analysis of the next clip while the current one encodes (same result, less waiting)."""
        key = (round(clip.start, 3), round(clip.end, 3))
        if self.video is None or key in self._analyses:
            return
        if self._analyzer is None:
            self._analyzer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clippy-analysis")
        self._analyses[key] = self._analyzer.submit(analyze, self.video, clip.start, clip.end - clip.start)

    def _prefetched(self, clip: Clip) -> ClipAnalysis | None:
        future = self._analyses.pop((round(clip.start, 3), round(clip.end, 3)), None)
        try:
            return future.result() if future else None
        except Exception as exc:
            log(f"[render] prefetched analysis failed ({type(exc).__name__}); analysing again")
            return None

    def render(self, clip: Clip, dest: Path, name: str, overrides: dict | None = None, keep_base: bool = False) -> dict:
        """dest/name.mp4 + .jpg cover + .txt post text; scratch files are removed afterwards. `keep_base`: the same
        pass also writes the editor's base (the frames without captions) as dest/.name.base.mkv, see info["base"]."""
        self.prepare()
        o = self.opts.merged(overrides)
        dest.mkdir(parents=True, exist_ok=True)
        settings = o.render_settings(clip.hook, self.audio_source(o.duck), self.caption_translator(o.captions, o))
        analysis = self._prefetched(clip)
        out_file = dest / f"{name}.mp4"
        work = self.storage.scratch(self.scratch_root)
        try:
            if keep_base:
                settings = replace(settings, base_out=work / "base.mkv")
            draft = work / out_file.name
            info = render_clip(self.video, self.words, clip.start, clip.end, draft, work,
                               prepare_fonts(self.storage.cache("fonts")), settings, analysis)
            if info.get("hook"):
                clip.hook = info["hook"]
            atomic_write_text(out_file.with_suffix(".txt"),
                              post_text(clip, self.source, info, Settings(self.storage).profile_links()))
            move_atomic(draft.with_suffix(".jpg"), out_file.with_suffix(".jpg"))
            covers.make(out_file.with_suffix(".jpg"), clip.title, clip.alt_title,
                        prepare_fonts(self.storage.cache("fonts")))
            move_atomic(draft, out_file)
            if keep_base and settings.base_out.is_file():
                info["base"].update(video=str(move_atomic(settings.base_out, dest / f".{name}.base.mkv")),
                                    layout=o.layout, punch=o.punch, tighten=o.tighten, audio=o.audio, duck=o.duck)
            else:
                info.pop("base", None)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        log(f"         {info['duration_out']:.1f} s, {info['jump_cuts']} jump cuts, {info['dead_air_removed']:.1f} s "
            f"dead air removed, {info['shots']} shots, layout: {info['layout_frames']}")
        return {**info, "mp4": str(out_file), "jpg": str(out_file.with_suffix(".jpg")),
                "txt": str(out_file.with_suffix(".txt"))}

    def transcript_text(self, start: float, end: float) -> str:
        return " ".join(w["word"] for w in self.words if start - 0.05 <= w["start"] < end)

    def save_plan(self, clips: list[Clip]) -> Path:
        path = self.storage.project(self.title) / "clips.json"
        write_json(path, {"source": self.source, "title": self.title, "llm": self.llm_desc,
                          "language": (self.transcript or {}).get("language"), "clips": [c.to_dict() for c in clips]})
        return path

    @staticmethod
    def load_plan(path: Path) -> tuple[str, list[Clip], dict]:
        plan = read_json(path)
        return plan["source"], [Clip.from_dict(c) for c in plan["clips"]], plan
