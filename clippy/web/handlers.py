"""What each kind of job does: every kind is a JobHandler that gets a JobContext (the chat, its clips and the ways
to render, store and show a clip version) and returns the text of its finished card."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import requests

from .. import covers, recovery
from ..concurrency import RENDER, GuardedLLM
from ..editing.render.base import BaseRender, BaseStore
from ..editing.repository import TimelineRepository
from ..highlights import Clip, text_language_name, write_texts
from ..i18n import t
from ..llm import create_llm
from ..pipeline import Options, Project, post_text
from ..publish import PostText, Upload
from ..publish.registry import LABELS, POSTING
from ..render import prepare_fonts
from ..settings import Settings
from ..tools import ClippyError, SharedProgress, atomic_write_text, log, report_span
from . import cover_retry, results, schedule
from .agent import RENDER_KEYS
from .clips import caption_of, slim_info
from .kit import StyleKit
from .progress import CLIP_STAGES
from .texts import NEW_TITLE, PLACEHOLDER_TITLE, english, tx

if TYPE_CHECKING:
    from .jobs import Runner

AUTO_POST_MIN = 65
GAMING = "20"


def job_text(kind: str, payload: dict) -> dict:
    clips = payload.get("clips")
    if kind == "undo" and isinstance(clips, list):
        return tx("job.undo", clip=clips[0])
    if kind == "edit" and isinstance(clips, list):
        return tx("job.edit", clip=clips[0]) if len(clips) == 1 else tx("job.edit_many", count=len(clips))
    return tx(f"job.{kind}", n=payload.get("n"), clip=payload.get("clip"),
              count=len(clips) if isinstance(clips, list) else None)


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
    def __init__(self, runner: Runner, job: Job):
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
        self.say(t(key, **params), data={"t": tx(key, **params)})

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

    def remember(self) -> None:
        progress = self.runner.progress.get(self.job.id)
        if progress is not None:
            progress.save()

    def show(self, row: dict) -> None:
        self.runner.hub.publish(self.job.chat_id, {"type": "clip",
                                                   "clip": self.runner.presenter.present(row, current=True)})

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
    def apply_kit(ctx: JobContext, proj: Project, first: int) -> int:
        """The style kit's logo and outro on the clips this job made (once, a retry skips what has it)."""
        ops = StyleKit(ctx.runner.storage).operations(ctx.job.chat_id)
        done = ctx.job.payload.setdefault("kit", [])
        rows = [c for c in ctx.current_clips() if c["number"] >= first and c["number"] not in done]
        if not ops or not rows:
            return 0
        applied = 0
        for i, row in enumerate(rows, 1):
            log(f"[render] style kit {i}/{len(rows)}  #{row['number']}")
            try:
                outcome = ctx.runner.edit_service(proj).apply(proj, ctx.chat, row, ops, t("kit.applied"))
            except ClippyError as exc:
                ctx.note("note.kit_failed", clip=row["number"], error=short_error(exc))
                continue
            applied += EditJob.report(ctx, row, outcome)
            done.append(row["number"])
            ctx.remember()
        return applied

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

        def keep(k: int, info: dict) -> None:
            row = ctx.store(first + k, 1, clips[k].to_dict(), overrides, info, info["mp4"], info["jpg"], info["txt"])
            ctx.keep_base(row, info)

        shared = SharedProgress(total)
        failed: list[tuple[int, Exception]] = []
        stored = 0
        with ThreadPoolExecutor(max_workers=min(RENDER.slots, total), thread_name_prefix="clippy-render") as pool:
            tasks = [pool.submit(shared.wrap(k, one), k, clip) for k, clip in enumerate(clips)]
            try:
                for k, task in enumerate(tasks):
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
                    keep(k, info)
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
                    keep(k, one(k, clips[k]))
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
        chat_opts = {**StyleKit(ctx.runner.storage).render_options(), **(chat.get("options") or {}),
                     **p.get("options", {})}
        ctx.db.update_chat(chat["id"], source=p["url"], options=chat_opts)
        proj = ctx.project(p["url"], ctx.options())
        proj.prepare()
        if getattr(proj, "is_stream", False) and chat_opts.get("layout", "auto") == "auto":
            log("stream: facecam on top, game below")
            chat_opts["layout"] = "stream"
            ctx.db.update_chat(chat["id"], options=chat_opts)
        title = p.get("title") or proj.title
        updates = {"video_title": title, "video_id": proj.video_stem}
        if chat["title"] in (NEW_TITLE, "", PLACEHOLDER_TITLE) or chat["title"].startswith("http"):
            updates["title"] = title[:80]
        ctx.runner.hub.publish(chat["id"], {"type": "chat", "chat": ctx.db.update_chat(chat["id"], **updates)})
        existing = [Clip.from_dict(c["clip"]) for c in ctx.current_clips()] if same_video else []
        clips = proj.select(n=p.get("n"), exclude=existing, instructions=p.get("instructions") or "",
                            lessons=results.lessons(results.insights(ctx.db)))
        first = ctx.db.next_number(chat["id"])
        n = self.render_new(ctx, proj, clips, render_options(chat_opts))
        self.apply_kit(ctx, proj, first)
        if p.get("autopost") and not p.get("planned"):
            numbers = [c["number"] for c in ctx.current_clips() if c["number"] >= first]
            planned = schedule.plan_in_slots(ctx.db, chat["id"], numbers, p["autopost"])
            p["planned"] = True
            ctx.remember()
            for row in ctx.current_clips():
                if row["number"] in numbers:
                    ctx.show(row)
            if planned:
                return tx("done.generate_planned", n=n, title=proj.title, count=len(planned))
        if p.get("post") and not p.get("posted"):
            self.post_new(ctx, first, p["post"])
            p["posted"] = True
            ctx.remember()
        if p.get("then_edit") and not p.get("edit_queued"):
            numbers = [c["number"] for c in ctx.current_clips() if c["number"] >= first]
            if numbers:
                ctx.runner.submit(chat["id"], "edit", {"type": "edit", "clips": numbers, **p["then_edit"]})
            p["edit_queued"] = True
            ctx.remember()
            if numbers:
                return tx("done.generate_then_edit", n=n, title=title)
        return tx("done.generate", n=n, title=title)

    @staticmethod
    def post_new(ctx, first: int, post: dict) -> None:
        """A daily pick's clip goes out at its time; past that time, right away. A weak pick (a test stream, a clip
        with nothing to show) is left for the user instead of going out by itself."""
        options = {"youtube": {"privacy": "public", "category": GAMING}}
        for row in ctx.current_clips():
            if row["number"] < first:
                continue
            score = float((row.get("clip") or {}).get("score") or 0)
            if score < AUTO_POST_MIN:
                ctx.note("daily.not_posted", clip=row["number"], score=round(score), min=AUTO_POST_MIN)
                continue
            if float(post["at"]) > time.time() + schedule.MIN_AHEAD:
                schedule.post_at(ctx.db, ctx.runner, ctx.job.chat_id, row["number"], post["platforms"], options,
                                 float(post["at"]))
            else:
                ctx.runner.submit(ctx.job.chat_id, "publish", {"clip": row["number"], "platforms": post["platforms"],
                                                              "options": options})


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
        clips = proj.select(n=p.get("n", 3), exclude=existing, instructions=p.get("instructions") or "", opts=opts,
                            lessons=results.lessons(results.insights(ctx.db)))
        first = ctx.db.next_number(chat["id"])
        n = self.render_new(ctx, proj, clips, render_options({**(chat.get("options") or {}), **p.get("options", {})}))
        self.apply_kit(ctx, proj, first)
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
            ctx.say(t(key, **params), data={"t": tx(key, **params), "reply": outcome.reply})
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
        changed = write_texts(GuardedLLM(create_llm(proj.opts.llm, proj.opts.model, ctx.runner.storage)), clips, proj.words,
                              proj.title, language)
        links = Settings(ctx.runner.storage).profile_links()
        for row, clip in zip(rows, clips):
            data = {**row["clip"], "title": clip.title, "description": clip.description, "question": clip.question,
                    "topic": clip.topic, "alt_title": clip.alt_title}
            stored = ctx.db.update_clip_texts(row["id"], data)
            if stored.get("jpg"):
                covers.make(Path(stored["jpg"]), clip.title, clip.alt_title,
                            prepare_fonts(ctx.runner.storage.cache("fonts")))
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
        row = ctx.db.current_clip(chat["id"], number)
        if row is None:
            raise ClippyError(key="error.no_clip", clip=number)
        proj = ctx.project(chat["source"], ctx.options())
        outcome = ctx.runner.edit_service(proj).undo(chat["id"], number)
        return tx("done.undo", clip=number) if EditJob.report(ctx, row, outcome) else tx("done.nothing")


def post_of(row: dict, variant: str = "a") -> PostText:
    """What goes up with the video: the clip's title (the B title for variant "b") and the caption part of its post
    text (not the notes)."""
    clip = Clip.from_dict(row["clip"])
    txt = Path(row["txt"]) if row.get("txt") else None
    caption = caption_of(txt.read_text(encoding="utf-8")) if txt and txt.is_file() else ""
    title = clip.alt_title if variant == "b" and clip.alt_title else clip.title
    return PostText(title, caption or clip.description or clip.title, tuple(clip.hashtags))


def problem_of(exc: Exception) -> dict:
    if isinstance(exc, ClippyError):
        return tx(exc.key, **exc.params) if exc.key else tx("error.plain", error=str(exc))
    return tx("error.unexpected", error=f"{type(exc).__name__}: {str(exc)[:160]}")


class PublishJob(JobHandler):
    """Posts one clip to the chosen platforms one after the other. One platform failing does not stop the others,
    and a platform that has the clip already is never posted to again by a retry or a restart (payload "done")."""

    kind = "publish"
    stages = ("publish",)

    def run(self, ctx: JobContext) -> dict:
        p, number = ctx.job.payload, int(ctx.job.payload.get("clip") or 0)
        row = ctx.db.current_clip(ctx.job.chat_id, number)
        if row is None or not row.get("mp4") or not Path(row["mp4"]).is_file():
            raise ClippyError(key="error.no_clip", clip=number)
        platforms = [x for x in dict.fromkeys(p.get("platforms") or []) if x in POSTING]
        if not platforms:
            raise ClippyError(key="error.no_platform")
        upload = Upload(Path(row["mp4"]), float((row.get("info") or {}).get("duration_out") or 0.0))
        post, accounts = post_of(row, (p.get("options") or {}).get("variant", "a")), ctx.runner.accounts
        done, failed = p.setdefault("done", {}), p.setdefault("failed", {})
        network = []
        for i, name in enumerate(platforms):
            if name in done or name in failed:
                continue
            log(f"[publish] {LABELS[name]}")
            with report_span(i / len(platforms), (i + 1) / len(platforms)):
                try:
                    result = accounts.platform(name).publish(accounts.http, accounts.tokens(name), upload, post,
                                                             (p.get("options") or {}).get(name) or {})
                except (ClippyError, requests.RequestException, OSError) as exc:
                    error = exc if isinstance(exc, ClippyError) else ClippyError(key="error.platform_network",
                                                                                 platform=LABELS[name])
                    ctx.db.add_post(row, name, "failed", problem=problem_of(error))
                    if error.key == "error.platform_network":
                        network.append(LABELS[name])
                    else:
                        failed[name] = problem_of(error)
                else:
                    if result.cover_pending:
                        log(f"[publish] {LABELS[name]} cover: trying again later")
                    at = ((p.get("options") or {}).get(name) or {}).get("publish_at")
                    ctx.db.add_post(row, name, result.state, result.url, result.remote_id,
                                    cover_due=cover_retry.first_try(time.time()) if result.cover_pending else 0.0,
                                    publish_at=float(at or 0.0) if result.state == "scheduled" else 0.0)
                    done[name] = result.state
                ctx.remember()
            ctx.show(row)
        if network:
            raise ClippyError(key="error.platform_network", platform=", ".join(network))
        if failed and not done:
            first = next(iter(failed.values()))
            raise ClippyError(key=first["key"], **first["params"])
        names = ", ".join(LABELS[n] for n in platforms if n in done)
        if failed:
            return tx("done.publish_some", clip=number, ok=names, failed=", ".join(LABELS[n] for n in failed))
        return tx("done.publish", clip=number, platforms=names)


HANDLERS: dict[str, JobHandler] = {h.kind: h for h in (GenerateJob(), MoreJob(), EditJob(), UndoJob(), TextsJob(),
                                                       PublishJob())}
