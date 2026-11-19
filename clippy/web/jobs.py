"""Background jobs on a few worker threads: one job per chat at a time (versions stay in order), different chats in
parallel; the GPU and the render slots are shared through clippy.concurrency. What a job does is its JobHandler."""

from __future__ import annotations

import itertools
import os
import threading
import time
import traceback
from typing import Callable

from .. import recovery
from ..concurrency import GuardedLLM
from ..editing.assets import AssetService
from ..editing.planner import EditPlanner
from ..editing.service import EditService
from ..llm import create_llm
from ..pipeline import Options, Project
from ..publish.accounts import AccountStore
from ..storage import Storage
from ..tools import ClippyError, log_sink
from .clips import ClipLibrary, ClipPresenter
from .handlers import HANDLERS, Job, JobContext, expected_clips, problem_of
from .hub import Hub
from .progress import Progress
from .texts import english, tx

WORKERS = max(1, int(os.environ.get("CLIPPY_WORKERS", "3")))


def default_edit_service(runner: Runner) -> Callable[[Project], EditService]:
    def build(project: Project) -> EditService:
        llm = GuardedLLM(create_llm(project.opts.llm, project.opts.model, runner.storage))
        return EditService(runner.db, runner.storage, AssetService(runner.storage), EditPlanner(llm))
    return build


class Runner:
    """Worker threads take the oldest waiting job whose chat has nothing running; the scheduler's own lock is held
    only for bookkeeping, never while a job runs, so it cannot take part in a deadlock."""

    def __init__(self, db, hub: Hub, storage: Storage, presenter: ClipPresenter, library: ClipLibrary,
                 project_factory=Project, start: bool = True, edit_service: Callable | None = None,
                 workers: int = WORKERS, accounts=None):
        self.db, self.hub, self.storage = db, hub, storage
        self._accounts = accounts
        self.edit_service = edit_service or default_edit_service(self)
        self.presenter, self.library, self.project_factory = presenter, library, project_factory
        self.projects: dict[int, Project] = {}
        self.pending: list[Job] = []
        self.running: list[Job] = []
        self._ids = itertools.count(1)
        self.progress: dict[int, Progress] = {}
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._stops: dict[int, threading.Event] = {}
        self._stops_lock = threading.Lock()
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
    def accounts(self):
        if self._accounts is None:
            self._accounts = AccountStore(self.storage)
        return self._accounts

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
        """A deleted chat: its project goes and its running job stops at its next step."""
        with self._lock:
            self.projects.pop(chat_id, None)
            for job in self.running + self.pending:
                if job.chat_id == chat_id:
                    self.stop_of(job).set()

    def stop_of(self, job: Job) -> threading.Event:
        with self._stops_lock:
            return self._stops.setdefault(job.id, threading.Event())

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
        with self._stops_lock:
            self._stops.pop(job.id, None)

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
            try:
                progress.finish(problem_of(exc), error=True)
            except Exception:
                pass
        finally:
            self._release(job)
            if self.db.get_chat(job.chat_id) is None:
                self.storage.purge_chat(job.chat_id)
                self.library.remove_chat(job.chat_id)
            self._broadcast_status()

    def _attempts(self, job: Job, progress: Progress) -> dict:
        """Runs the job; a failure that another attempt may fix starts it again on the same card, with the settings
        the diagnosis changes (they are kept in the payload, so a restart keeps them too)."""
        attempt = 1
        while True:
            try:
                with log_sink(progress.line, progress.report, self.stop_of(job)):
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
