"""Background jobs on a few worker threads: one job per chat at a time (versions stay in order), different chats in
parallel; the GPU and the render slots are shared through clippy.concurrency. Every job kind is a JobHandler."""

from __future__ import annotations

import itertools
import os
import re
import threading
import time
import traceback
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import recovery
from ..concurrency import RENDER, GuardedLLM
from ..editing.assets import AssetService
from ..editing.planner import EditPlanner
from ..editing.render.base import BaseRender, BaseStore
from ..editing.repository import TimelineRepository
from ..editing.service import EditService
from ..highlights import Clip, text_language_name, write_texts
from ..settings import Settings
from ..llm import create_llm
from ..i18n import t
from ..pipeline import Options, Project, post_text
from ..storage import Storage
from ..tools import ClippyError, SharedProgress, atomic_write_text, log, log_sink
from .agent import RENDER_KEYS
from .clips import ClipLibrary, ClipPresenter, slim_info
from .hub import Hub

PLACEHOLDER_TITLE = "Processing video…"
WORKERS = max(1, int(os.environ.get("CLIPPY_WORKERS", "3")))


def tx(key: str, **params) -> dict:
    """A translatable text: the browser renders it in the UI language, the stored content is English."""
    return {"key": key, "params": {k: v for k, v in params.items() if v is not None}}


def english(tx: dict) -> str:
    return t("en", tx["key"], **tx["params"])


def job_text(kind: str, payload: dict) -> dict:
    clips = payload.get("clips")
    if kind == "undo" and isinstance(clips, list):
        return tx("job.undo", clip=clips[0])
    if kind == "edit" and isinstance(clips, list):
        return tx("job.edit", clip=clips[0]) if len(clips) == 1 else tx("job.edit_many", count=len(clips))
    return tx(f"job.{kind}", n=payload.get("n"), clip=payload.get("clip"),
              count=len(clips) if isinstance(clips, list) else None)


@dataclass(frozen=True)
class Stage:
    key: str
    label: str
    prefixes: tuple[str, ...]
    weight: float


STAGES = (
    Stage("download", "Download", ("[download]",), 10),
    Stage("audio", "Voice", ("[audio]",), 7),
    Stage("transcribe", "Transcript", ("[transcribe]",), 30),
    Stage("align", "Timing", ("[align]",), 3),
    Stage("select", "Picking", ("[hook]", "[plan]"), 40),
    Stage("render", "Render", ("[faces]", "[render]"), 0),
    Stage("edit", "Edit", ("[edit]", "[render]", "[faces]"), 10),
)
CLIP_STAGES = ("download", "audio", "transcribe", "align", "select", "render")
RENDER_WEIGHT_PER_CLIP = 5
STAGE_BY_KEY = {st.key: st for st in STAGES}
CACHE_MARKERS = ("cache", "already here")
RENDER_COUNTER = re.compile(r"^\[render\]\s+(\d+)/(\d+)")
WINDOW_COUNTER = re.compile(r"^\[hook\] window (\d+)/(\d+)")


@dataclass(frozen=True)
class Job:
    id: int
    chat_id: int
    kind: str
    payload: dict

    @property
    def label_t(self) -> dict:
        return job_text(self.kind, self.payload)

    @property
    def label(self) -> str:
        return english(self.label_t)


class Progress:
    """One live progress card in the chat: queue position, stage stepper, percentage and a calibrated ETA."""

    PUBLISH_EVERY = 0.4
    SAVE_EVERY = 2.0

    def __init__(self, db, hub: Hub, chat_id: int, label_t: dict, stages: tuple[str, ...], clips: int = 1,
                 job: dict | None = None):
        self.db, self.hub, self.chat_id, self.label_t, self.job = db, hub, chat_id, label_t, job
        self.weight = {st.key: st.weight for st in STAGES}
        self.weight["render"] = RENDER_WEIGHT_PER_CLIP * max(1, clips)
        self.created = time.time()
        self.started: float | None = None
        self.plan = [STAGE_BY_KEY[k] for k in stages]
        self.state = {st.key: "pending" for st in self.plan}
        self.fraction = {st.key: 0.0 for st in self.plan}
        self.entered: dict[str, float] = {}
        self.cached_time = 0.0
        self.active: str | None = None
        self.queue_position: int | None = None
        self.detail, self.detail_t, self.lines = "", None, []
        self.eta: float | None = None
        self._published = self._saved = 0.0
        self._lock = threading.RLock()
        self.message = db.add_message(chat_id, "assistant", english(label_t), kind="progress", data=self.data(False))
        hub.publish(chat_id, {"type": "message", "message": self.message})

    def stage_label(self) -> str:
        if self.active:
            return STAGE_BY_KEY[self.active].label
        return "Starting" if self.started else "Waiting"

    def data(self, done: bool, **extra) -> dict:
        return {
            "phase": "done" if done else ("running" if self.started else "queued"),
            "queue_position": self.queue_position,
            "stage": self.stage_label(),
            "stages": [{"key": st.key, "label": st.label, "state": self.state[st.key],
                        "fraction": round(self.fraction[st.key], 4)} for st in self.plan],
            "percent": round(100 * (1.0 if done else self.overall()), 1),
            "eta": None if done or self.eta is None else round(self.eta),
            "detail": self.detail, "detail_t": self.detail_t, "label_t": self.label_t, "lines": self.lines, "done": done,
            "created": self.created, "started": self.started, **({"job": self.job} if self.job else {}), **extra,
        }

    def queued(self, position: int) -> None:
        with self._lock:
            if position != self.queue_position:
                self.queue_position = position
                self._push(force=True)

    def begin(self) -> None:
        with self._lock:
            self.started = time.time()
            self.queue_position = None
            if self.plan:
                self._enter(self.plan[0].key)
            self._push(force=True)

    def overall(self) -> float:
        live = [st for st in self.plan if self.state[st.key] not in ("cached", "skipped")]
        total = sum(self.weight[st.key] for st in live)
        return sum(self.weight[st.key] * self.fraction[st.key] for st in live) / total if total else 0.0

    def line(self, text: str) -> None:
        with self._lock:
            self._line(text)

    def _line(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.lines = (self.lines + [text])[-14:]
        stage = next((st for st in self.plan if text.startswith(st.prefixes)), None)
        if stage is None and any(text.startswith(st.prefixes) for st in STAGES):
            self._detail(tx("detail.loading"))
        elif stage is not None and self._advance(stage.key):
            if any(m in text.lower() for m in CACHE_MARKERS) and self.fraction[stage.key] < 0.05:
                self._mark_cached(stage.key)
            if m := RENDER_COUNTER.match(text):
                self._detail(tx("detail.clip", a=int(m.group(1)), b=int(m.group(2))))
                self.weight["render"] = RENDER_WEIGHT_PER_CLIP * int(m.group(2))
            elif m := WINDOW_COUNTER.match(text):
                self._detail(tx("detail.window", a=int(m.group(1)), b=int(m.group(2))))
        self._push()

    def report(self, fraction: float) -> None:
        with self._lock:
            if self.active and self.state[self.active] == "active":
                self.fraction[self.active] = max(self.fraction[self.active], min(1.0, fraction))
                self._push()

    def _detail(self, tx: dict | None) -> None:
        self.detail_t = tx
        self.detail = english(tx) if tx else ""

    def retry(self, reason_t: dict, attempt: int) -> None:
        """The card stays: it says why the job starts again and which attempt this is."""
        with self._lock:
            self._line(f"[retry] {english(reason_t)} ({attempt}/{recovery.MAX_ATTEMPTS})")
            self._detail(tx("detail.retry", n=attempt, max=recovery.MAX_ATTEMPTS))
            if self.active:
                self.state[self.active] = "active"
            self._push(force=True)

    def finish(self, summary_t: dict, error: bool = False) -> None:
        with self._lock:
            self._finish(summary_t, error)

    def _finish(self, summary_t: dict, error: bool) -> None:
        now = time.time()
        if error:
            if self.active:
                self.state[self.active] = "failed"
        else:
            for st in self.plan:
                if self.state[st.key] in ("pending", "active"):
                    entered = st.key in self.entered
                    self.state[st.key], self.fraction[st.key] = ("done", 1.0) if entered else ("skipped", 0.0)
        self.active = None
        data = self.data(True, finished=now, started=self.started or now, summary_t=summary_t,
                         **({"error": True} if error else {}))
        msg = self.db.update_message(self.message["id"], content=english(summary_t), kind="error" if error else None,
                                     data=data)
        self.hub.publish(self.chat_id, {"type": "message", "message": msg})

    def _enter(self, key: str) -> None:
        self.active = key
        self.state[key] = "active"
        self.entered[key] = time.time()
        self._detail(None)

    def _advance(self, key: str) -> bool:
        """Stages only move forward; False when the line belongs to an earlier, finished stage."""
        order = [st.key for st in self.plan]
        if self.active == key:
            return True
        if self.active and order.index(key) < order.index(self.active):
            return False
        for k in order[:order.index(key)]:
            if self.state[k] == "active":
                self.state[k], self.fraction[k] = "done", 1.0
            elif self.state[k] == "pending":
                self.state[k] = "skipped"
        self._enter(key)
        return True

    def _mark_cached(self, key: str) -> None:
        self.state[key], self.fraction[key] = "cached", 1.0
        self.cached_time += time.time() - self.entered.get(key, time.time())

    def _estimate(self) -> float | None:
        """Remaining seconds: the active stage extrapolated from its own pace, later stages at the pace so far."""
        if not self.started:
            return None
        now = time.time()
        live = [st for st in self.plan if self.state[st.key] not in ("cached", "skipped")]
        done_w = sum(self.weight[st.key] * self.fraction[st.key] for st in live)
        left_w = sum(self.weight[st.key] * (1 - self.fraction[st.key]) for st in live)
        spent = now - self.started - self.cached_time
        if done_w < 1.5 or spent < 8:
            return None
        rate = spent / done_w
        if self.active and self.state[self.active] == "active":
            f = self.fraction[self.active]
            in_stage = now - self.entered[self.active]
            if f > 0.08 and in_stage > 5:
                later_w = left_w - self.weight[self.active] * (1 - f)
                return max(0.0, in_stage * (1 - f) / f + later_w * rate)
        return max(0.0, left_w * rate)

    def _push(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._published < self.PUBLISH_EVERY:
            return
        estimate = self._estimate()
        if estimate is None or self.eta is None:
            self.eta = estimate
        else:
            self.eta = 0.7 * max(0.0, self.eta - (now - self._published)) + 0.3 * estimate
        self._published = now
        data = self.data(False)
        self.hub.publish(self.chat_id, {"type": "progress", "message_id": self.message["id"], "data": data})
        if force or now - self._saved > self.SAVE_EVERY:
            self.db.update_message(self.message["id"], data=data)
            self._saved = now


def short_error(exc: Exception) -> str:
    if isinstance(exc, ClippyError) and exc.key:
        return english(tx(exc.key, **exc.params))
    return f"{type(exc).__name__}: {str(exc)[:160]}"


def expected_clips(payload: dict) -> int:
    clips = payload.get("clips")
    if isinstance(clips, list):
        return len(clips)
    return int(payload.get("n") or 1)


class JobContext:
    def __init__(self, runner: "Runner", job: Job):
        self.runner, self.job, self.db = runner, job, runner.db
        self._gallery: dict | None = None

    @property
    def chat(self) -> dict:
        chat = self.db.get_chat(self.job.chat_id)
        if chat is None:
            raise ClippyError(key="error.chat_deleted")
        return chat

    def options(self, extra: dict | None = None) -> Options:
        """The chat's settings, the job's own, then what earlier failed attempts changed (see recovery)."""
        return Options.from_dict({**(self.chat.get("options") or {}), **(extra or {}),
                                  **(self.job.payload.get("fixes") or {})})

    def note(self, key: str, **params) -> None:
        self.say(t("en", key, **params), data={"t": tx(key, **params)})

    def project(self, source: str, opts: Options) -> Project:
        return self.runner.project_for(self.job.chat_id, source, opts)

    def current_clips(self) -> list[dict]:
        return self.db.current_clips(self.job.chat_id)

    def render(self, proj: Project, clip: Clip, number: int, version: int, overrides: dict) -> dict:
        """The files of a new version; nothing is stored until `store`, the commit point."""
        return proj.render(clip, self.runner.storage.chat_clips(self.job.chat_id), f"clip_{number:02d}_v{version}",
                           overrides, keep_base=True)

    def keep_base(self, row: dict, info: dict) -> None:
        """The editor's base made in the same pass as a new clip goes into the base cache, so the clip's first edit
        skips the base render. Kept only when the editor's timeline asks for exactly these frames."""
        base = info.get("base") or {}
        if not base.get("video"):
            return
        video = Path(base["video"])
        try:
            timeline = TimelineRepository(self.db).of(row, self.chat.get("options") or {})
            cut, camera, sound = timeline.cut, timeline.camera, timeline.audio
            same = (not cut.removed and (cut.source.start, cut.source.end) == (row["clip"]["start"], row["clip"]["end"])
                    and (camera.layout, camera.punch, camera.tighten) == (base["layout"], base["punch"], base["tighten"])
                    and (sound.mode, sound.source_music_duck_db) == (base["audio"], float(base["duck"] or 0.0)))
            if same and video.is_file():
                slim = {k: v for k, v in info.items() if k not in ("base", "mp4", "jpg", "txt")}
                BaseStore(self.runner.storage.chat_base(self.job.chat_id)).put(timeline.base_key(), BaseRender(
                    video, float(info["duration_out"]), base["fps"], base["rate"], base["words"],
                    tuple(tuple(i) for i in base["intervals"]), {**slim, "hook": ""}))
        finally:
            video.unlink(missing_ok=True)

    def store(self, number: int, version: int, clip: dict, overrides: dict, info: dict, mp4: str, jpg: str, txt: str,
              timeline: str | None = None) -> dict:
        """Saves a clip version, copies it to the library and shows it in the chat."""
        row = self.db.add_clip(self.job.chat_id, number, version, clip, overrides, slim_info(info), mp4, jpg, txt,
                               timeline)
        self.runner.library.publish(self.chat, row)
        self._show(row)
        return row

    def say(self, content: str, kind: str = "text", data: dict | None = None) -> None:
        msg = self.db.add_message(self.job.chat_id, "assistant", content, kind=kind, data=data)
        self.runner.hub.publish(self.job.chat_id, {"type": "message", "message": msg})

    def _show(self, row: dict) -> None:
        chat_id = self.job.chat_id
        if self._gallery is None:
            self._gallery = self.db.add_message(chat_id, "assistant", "", kind="clips", data={"clip_ids": [row["id"]]})
        else:
            ids = self._gallery["data"]["clip_ids"] + [row["id"]]
            self._gallery = self.db.update_message(self._gallery["id"], data={"clip_ids": ids})
        self.runner.hub.publish(chat_id, {"type": "clip", "clip": self.runner.presenter.present(row, current=True)})
        self.runner.hub.publish(chat_id, {"type": "message", "message": self._gallery})


def render_options(opts: dict) -> dict:
    return {k: v for k, v in opts.items() if k in RENDER_KEYS}


class JobHandler(ABC):
    kind = ""
    stages: tuple[str, ...] = ("render",)

    @abstractmethod
    def run(self, ctx: JobContext) -> dict: ...

    @staticmethod
    def render_new(ctx: JobContext, proj: Project, clips: list[Clip], overrides: dict) -> int:
        """Clips render side by side (as many as there are render slots) but are stored in order, one by one, each
        all or nothing; the numbers are reserved up front, which is safe because a chat runs one job at a time."""
        first, total = ctx.db.next_number(ctx.job.chat_id), len(clips)
        for clip in clips[RENDER.slots:]:
            proj.prefetch(clip)

        def one(k: int, clip: Clip) -> dict:
            log(f"[render] {k + 1}/{total}  #{first + k}")
            return ctx.render(proj, clip, first + k, 1, overrides)

        shared = SharedProgress(total)
        failed: list[tuple[int, Exception]] = []
        stored = 0
        with ThreadPoolExecutor(max_workers=min(RENDER.slots, total), thread_name_prefix="clippy-render") as pool:
            tasks = [pool.submit(shared.wrap(k, one), k, clip) for k, clip in enumerate(clips)]
            try:
                for k, (clip, task) in enumerate(zip(clips, tasks)):
                    try:
                        info = task.result()
                    except ClippyError as exc:
                        if exc.key == "error.chat_deleted":
                            raise
                        failed.append((k, exc))
                        continue
                    except Exception as exc:
                        failed.append((k, exc))
                        continue
                    row = ctx.store(first + k, 1, clip.to_dict(), overrides, info, info["mp4"], info["jpg"], info["txt"])
                    ctx.keep_base(row, info)
                    stored += 1
            except BaseException:
                for task in tasks:
                    task.cancel()
                raise
        lost: list[tuple[int, Exception]] = []
        for k, exc in failed:
            fix = recovery.diagnose(exc, proj.opts.to_dict())
            if fix is not None:
                log(f"[retry] clip #{first + k} failed ({type(exc).__name__}); once more: {fix.reason}")
                proj.opts = proj.opts.merged(fix.options)
                try:
                    info = one(k, clips[k])
                    row = ctx.store(first + k, 1, clips[k].to_dict(), overrides, info, info["mp4"], info["jpg"],
                                    info["txt"])
                    ctx.keep_base(row, info)
                    stored += 1
                    continue
                except Exception as again:
                    exc = again
            lost.append((k, exc))
        if lost and not stored:
            raise lost[0][1]
        for k, exc in lost:
            ctx.note("note.clip_failed", clip=first + k, error=short_error(exc))
        return stored


class GenerateJob(JobHandler):
    kind = "generate"
    stages = CLIP_STAGES

    def run(self, ctx: JobContext) -> dict:
        p, chat = ctx.job.payload, ctx.chat
        same_video = chat.get("source") == p["url"]
        chat_opts = {**(chat.get("options") or {}), **p.get("options", {})}
        ctx.db.update_chat(chat["id"], source=p["url"], options=chat_opts)
        proj = ctx.project(p["url"], ctx.options())
        proj.prepare()
        updates = {"video_title": proj.title, "video_id": proj.video_stem}
        if chat["title"] in ("New chat", "", PLACEHOLDER_TITLE) or chat["title"].startswith("http"):
            updates["title"] = proj.title[:80]
        ctx.runner.hub.publish(chat["id"], {"type": "chat", "chat": ctx.db.update_chat(chat["id"], **updates)})
        existing = [Clip.from_dict(c["clip"]) for c in ctx.current_clips()] if same_video else []
        clips = proj.select(n=p.get("n"), exclude=existing, instructions=p.get("instructions") or "")
        n = self.render_new(ctx, proj, clips, render_options(chat_opts))
        return tx("done.generate", n=n, title=proj.title)


class MoreJob(JobHandler):
    kind = "more"
    stages = ("select", "render")

    def run(self, ctx: JobContext) -> dict:
        p, chat = ctx.job.payload, ctx.chat
        if not chat.get("source"):
            raise ClippyError(key="error.no_video")
        opts = ctx.options(p.get("options"))
        proj = ctx.project(chat["source"], opts)
        existing = [Clip.from_dict(c["clip"]) for c in ctx.current_clips()]
        clips = proj.select(n=p.get("n", 3), exclude=existing, instructions=p.get("instructions") or "", opts=opts)
        n = self.render_new(ctx, proj, clips, render_options({**(chat.get("options") or {}), **p.get("options", {})}))
        return tx("done.more", n=n)


class EditJob(JobHandler):
    """Any change to existing clips: the edit service plans it with the LLM and renders the new versions."""

    kind = "edit"
    stages = ("edit",)

    def run(self, ctx: JobContext) -> dict:
        p, chat = ctx.job.payload, ctx.chat
        if not chat.get("source"):
            raise ClippyError(key="error.no_video")
        rows = {c["number"]: c for c in ctx.current_clips()}
        done = p.setdefault("done", [])
        numbers = list(dict.fromkeys(n for n in p["clips"] if n in rows and n not in done))
        if not numbers and not done:
            raise ClippyError(key="error.no_clip", clip=", ".join(map(str, p["clips"])))
        proj = ctx.project(chat["source"], ctx.options()).prepare()
        attached = tuple(p.get("attachments") or ())

        def one(i: int, number: int):
            log(f"[edit] clip #{number} ({i + 1}/{len(numbers)})")
            return ctx.runner.edit_service(proj).edit(proj, chat, rows[number], p["request"], attached)

        edited, shared, failed = len(done), SharedProgress(max(1, len(numbers))), []
        with ThreadPoolExecutor(max_workers=max(1, min(RENDER.slots, len(numbers))),
                                thread_name_prefix="clippy-edit") as pool:
            tasks = [pool.submit(shared.wrap(i, one), i, number) for i, number in enumerate(numbers)]
            try:
                for number, task in zip(numbers, tasks):
                    try:
                        outcome = task.result()
                    except Exception as exc:
                        failed.append((number, exc))
                        continue
                    edited += self.report(ctx, rows[number], outcome)
                    done.append(number)
            except BaseException:
                for task in tasks:
                    task.cancel()
                raise
        if failed and len(failed) == len(numbers):
            raise failed[0][1]
        for number, exc in failed:
            ctx.note("note.edit_failed", clip=number, error=short_error(exc))
        if not edited:
            return tx("done.nothing")
        return tx("done.edit", clip=numbers[0]) if len(numbers) == 1 else tx("done.edit_many", count=edited)

    @staticmethod
    def report(ctx: JobContext, row: dict, outcome) -> int:
        if outcome.question:
            ctx.say(outcome.reply or outcome.question)
            return 0
        if outcome.notice or outcome.errors:
            key, params = (outcome.notice, {}) if outcome.notice else ("edit.failed", {"detail": outcome.errors[0]})
            ctx.say(t("en", key, **params), data={"t": tx(key, **params), "reply": outcome.reply})
            return 0
        if outcome.version is None:
            if outcome.reply:
                ctx.say(outcome.reply)
            return 0
        v = outcome.version
        stored = ctx.store(row["number"], row["version"] + 1, v.clip, row.get("overrides") or {}, v.info, v.mp4, v.jpg,
                           v.txt, v.timeline)
        ctx.say(outcome.reply, kind="changes", data={"number": row["number"], "version": stored["version"],
                                                     "changes": [c.model_dump() for c in outcome.changes]})
        return 1


class TextsJob(JobHandler):
    """New titles and descriptions for existing clips: the LLM writes them from what is said in each clip; the post
    text file and the library copy follow, the video stays as it is."""

    kind = "texts"
    stages = ("edit",)

    def run(self, ctx: JobContext) -> dict:
        p, chat = ctx.job.payload, ctx.chat
        if not chat.get("source"):
            raise ClippyError(key="error.no_video")
        rows = [c for c in ctx.current_clips() if c["number"] in set(p.get("clips") or [])]
        if not rows:
            raise ClippyError(key="error.no_clip", clip=", ".join(map(str, p.get("clips") or [])))
        proj = ctx.project(chat["source"], ctx.options()).prepare()
        clips = [Clip.from_dict(row["clip"]) for row in rows]
        log(f"[edit] writing titles and descriptions for {len(clips)} clip(s)")
        language = text_language_name(ctx.options().captions, proj.spoken_language)
        changed = write_texts(GuardedLLM(create_llm(proj.opts.llm, proj.opts.model)), clips, proj.words,
                              proj.title, language)
        links = Settings(ctx.runner.storage).profile_links()
        for row, clip in zip(rows, clips):
            data = {**row["clip"], "title": clip.title, "description": clip.description, "question": clip.question,
                    "topic": clip.topic}
            stored = ctx.db.update_clip_texts(row["id"], data)
            if stored.get("txt"):
                atomic_write_text(Path(stored["txt"]), post_text(clip, chat["source"], stored["info"] or {}, links))
            ctx.runner.library.publish(chat, stored)
            ctx.runner.hub.publish(chat["id"], {"type": "clip",
                                                "clip": ctx.runner.presenter.present(stored, current=True)})
        return tx("done.texts", count=changed)


class UndoJob(JobHandler):
    """The previous version of a clip comes back as a new version, without the LLM and without rendering."""

    kind = "undo"
    stages = ("edit",)

    def run(self, ctx: JobContext) -> dict:
        chat, number = ctx.chat, ctx.job.payload["clips"][0]
        row = next((c for c in ctx.current_clips() if c["number"] == number), None)
        if row is None:
            raise ClippyError(key="error.no_clip", clip=number)
        proj = ctx.project(chat["source"], ctx.options())
        outcome = ctx.runner.edit_service(proj).undo(chat["id"], number)
        return tx("done.undo", clip=number) if EditJob.report(ctx, row, outcome) else tx("done.nothing")


HANDLERS: dict[str, JobHandler] = {h.kind: h for h in (GenerateJob(), MoreJob(), EditJob(), UndoJob(), TextsJob())}


def default_edit_service(runner: "Runner") -> Callable[[Project], EditService]:
    def build(project: Project) -> EditService:
        llm = GuardedLLM(create_llm(project.opts.llm, project.opts.model))
        return EditService(runner.db, runner.storage, AssetService(runner.storage), EditPlanner(llm))
    return build


class Runner:
    """Worker threads take the oldest waiting job whose chat has nothing running; the scheduler's own lock is held
    only for bookkeeping, never while a job runs, so it cannot take part in a deadlock."""

    def __init__(self, db, hub: Hub, storage: Storage, presenter: ClipPresenter, library: ClipLibrary,
                 project_factory=Project, start: bool = True, edit_service: Callable | None = None,
                 workers: int = WORKERS):
        self.db, self.hub, self.storage = db, hub, storage
        self.edit_service = edit_service or default_edit_service(self)
        self.presenter, self.library, self.project_factory = presenter, library, project_factory
        self.projects: dict[int, Project] = {}
        self.pending: list[Job] = []
        self.running: list[Job] = []
        self._ids = itertools.count(1)
        self.progress: dict[int, Progress] = {}
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        if start:
            self.resume()
            for i in range(workers):
                threading.Thread(target=self._loop, name=f"clippy-worker-{i + 1}", daemon=True).start()

    def resume(self) -> int:
        """Jobs the last server left unfinished (a restart, a crash) start again; their old cards say so. A card that
        does not carry its job takes it from the reply that started it, when that reply started exactly one job;
        otherwise it asks for the request again. Returns how many jobs were started again."""
        started = 0
        for row in self.db.unfinished_jobs():
            data = row.get("data") or {}
            job = data.get("job") or self._job_of_reply(row)
            alive = self.db.get_chat(row["chat_id"]) is not None
            again = alive and isinstance(job, dict) and job.get("kind") in HANDLERS
            summary = tx("job.interrupted" if again else "job.interrupted_lost")
            for st in data.get("stages") or []:
                if st.get("state") == "active":
                    st["state"] = "failed"
            data.update(phase="done", done=True, error=True, eta=None, summary_t=summary, finished=time.time())
            self.db.update_message(row["id"], content=english(summary), kind="error", data=data)
            if again:
                self.submit(row["chat_id"], job["kind"], job.get("payload") or {})
                started += 1
        return started

    def _job_of_reply(self, card: dict) -> dict | None:
        before = [m for m in self.db.list_messages(card["chat_id"]) if m["id"] < card["id"] and m["role"] == "assistant"
                  and m["kind"] == "text"]
        actions = [a for a in ((before[-1].get("data") or {}).get("actions") or [] if before else [])
                   if isinstance(a, dict) and a.get("type") in HANDLERS]
        return {"kind": actions[0]["type"], "payload": actions[0]} if len(actions) == 1 else None

    @property
    def current(self) -> Job | None:
        with self._lock:
            return self.running[0] if self.running else None

    def submit(self, chat_id: int, kind: str, payload: dict) -> Job:
        if kind not in HANDLERS:
            raise ValueError(kind)
        job = Job(next(self._ids), chat_id, kind, payload)
        progress = Progress(self.db, self.hub, chat_id, job.label_t, HANDLERS[kind].stages, expected_clips(payload),
                            job={"kind": kind, "payload": payload})
        with self._wake:
            self.pending.append(job)
            self.progress[job.id] = progress
            self._wake.notify_all()
        self._broadcast_status()
        return job

    def status(self) -> dict:
        with self._lock:
            running = [self._describe(j) for j in self.running]
            return {
                "busy": bool(running),
                "current": running[0] if running else None,
                "running": running,
                "queue": [{"chat_id": j.chat_id, "label": j.label, "label_t": j.label_t} for j in self.pending],
            }

    def _describe(self, job: Job) -> dict:
        out = {"chat_id": job.chat_id, "label": job.label, "label_t": job.label_t}
        progress = self.progress.get(job.id)
        if progress is not None and progress.started:
            out.update(stage=progress.stage_label(), stage_key=progress.active, percent=round(100 * progress.overall(), 1),
                       eta=progress.eta)
        return out

    def is_running(self, chat_id: int) -> bool:
        with self._lock:
            return any(j.chat_id == chat_id for j in self.running)

    def sources_in_use(self) -> set[str]:
        """Video ids that a waiting or running job may still read (their cache must not be purged)."""
        with self._lock:
            chats = {j.chat_id for j in self.running + self.pending}
            return {p.video_stem for c, p in self.projects.items() if c in chats and p.video_stem}

    def project_for(self, chat_id: int, source: str, opts: Options) -> Project:
        with self._lock:
            proj = self.projects.get(chat_id)
            if proj is None or proj.source != source:
                proj = self.project_factory(source, opts, self.storage, self.storage.chat_work(chat_id))
                self.projects[chat_id] = proj
            proj.opts = opts
            return proj

    def forget(self, chat_id: int) -> None:
        with self._lock:
            self.projects.pop(chat_id, None)

    def _take(self) -> Job:
        with self._wake:
            while True:
                busy = {j.chat_id for j in self.running}
                job = next((j for j in self.pending if j.chat_id not in busy), None)
                if job is not None:
                    self.pending.remove(job)
                    self.running.append(job)
                    return job
                self._wake.wait()

    def _release(self, job: Job) -> None:
        with self._wake:
            if job in self.running:
                self.running.remove(job)
            if job in self.pending:
                self.pending.remove(job)
            self.progress.pop(job.id, None)
            self._wake.notify_all()

    def run_one(self, job: Job) -> None:
        with self._lock:
            if job in self.pending:
                self.pending.remove(job)
            if job not in self.running:
                self.running.append(job)
            progress = self.progress.get(job.id)
        if progress is None:
            progress = Progress(self.db, self.hub, job.chat_id, job.label_t, HANDLERS[job.kind].stages,
                                expected_clips(job.payload))
            with self._lock:
                self.progress[job.id] = progress
        progress.begin()
        self._broadcast_status()
        try:
            summary = self._attempts(job, progress)
            progress.finish(summary or job.label_t)
        except Exception as exc:
            if not isinstance(exc, ClippyError):
                traceback.print_exc()
            if isinstance(exc, ClippyError):
                message = tx(exc.key, **exc.params) if exc.key else tx("error.plain", error=str(exc))
            else:
                message = tx("error.unexpected", error=f"{type(exc).__name__}: {exc}")
            try:
                progress.finish(message, error=True)
            except Exception:
                pass
        finally:
            self._release(job)
            if self.db.get_chat(job.chat_id) is None:
                self.storage.purge_chat(job.chat_id)
                self.library.remove_chat(job.chat_id)
            self._broadcast_status()

    def _attempts(self, job: Job, progress: "Progress") -> dict:
        """Runs the job; a failure that another attempt may fix starts it again on the same card, with the settings
        the diagnosis changes (they are kept in the payload, so a restart keeps them too)."""
        attempt = 1
        while True:
            try:
                with log_sink(progress.line, progress.report):
                    return HANDLERS[job.kind].run(JobContext(self, job))
            except Exception as exc:
                fixes = job.payload.get("fixes") or {}
                fix = recovery.diagnose(exc, fixes) if attempt < recovery.MAX_ATTEMPTS else None
                if fix is None or self.db.get_chat(job.chat_id) is None:
                    raise
                if not isinstance(exc, ClippyError):
                    traceback.print_exc()
                attempt += 1
                job.payload["fixes"] = {**fixes, **fix.options}
                progress.retry(tx(f"retry.{fix.reason}"), attempt)
                time.sleep(self.retry_pause(attempt))

    def retry_pause(self, attempt: int) -> float:
        return recovery.pause(attempt)

    def _loop(self) -> None:
        while True:
            job = self._take()
            try:
                if self.db.get_chat(job.chat_id) is None:
                    self._release(job)
                    self._broadcast_status()
                    continue
                self.run_one(job)
            except Exception:
                traceback.print_exc()
                self._release(job)

    def _broadcast_status(self) -> None:
        with self._lock:
            waiting = [(self.progress.get(j.id), n) for n, j in enumerate(self.pending, 1)]
        for progress, position in waiting:
            if progress is not None:
                try:
                    progress.queued(position)
                except Exception:
                    pass
        self.hub.publish(None, {"type": "status", "status": self.status()})
