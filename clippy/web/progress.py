"""The live progress card of a job: queue position, stage stepper, percentage and a calibrated ETA, fed by the
job's log lines and its `tools.report` fractions."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

from .. import recovery
from ..tools import log
from .hub import Hub
from .texts import english, tx


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
    Stage("publish", "Posting", ("[publish]",), 10),
)
CLIP_STAGES = ("download", "audio", "transcribe", "align", "select", "render")
RENDER_WEIGHT_PER_CLIP = 5
STAGE_BY_KEY = {st.key: st for st in STAGES}
CACHE_MARKERS = ("cache", "already here")
RENDER_COUNTER = re.compile(r"^\[render\]\s+(\d+)/(\d+)")
WINDOW_COUNTER = re.compile(r"^\[hook\] window (\d+)/(\d+)")


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
        self.spent: dict[str, float] = {}
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
                        "fraction": round(self.fraction[st.key], 4),
                        **({"seconds": round(self.spent[st.key], 1)} if st.key in self.spent else {})} for st in self.plan],
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

    def save(self) -> None:
        """Stores the card now, with its job: what the job wrote into its payload survives a restart."""
        with self._lock:
            self._push(force=True)

    def finish(self, summary_t: dict, error: bool = False) -> None:
        with self._lock:
            self._finish(summary_t, error)

    def _finish(self, summary_t: dict, error: bool) -> None:
        now = time.time()
        self._close(now)
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
        if self.spent:
            log("[timing] " + ", ".join(f"{k} {v:.0f} s" for k, v in self.spent.items()))

    def _close(self, now: float) -> None:
        """The active stage's time so far goes into its total (a stage entered again after a retry adds up)."""
        if self.active and self.active in self.entered:
            self.spent[self.active] = self.spent.get(self.active, 0.0) + now - self.entered[self.active]

    def _enter(self, key: str) -> None:
        self._close(time.time())
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
