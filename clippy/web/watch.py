"""Watched channels and planned posts. One background loop checks the watched channels for new videos (each one
becomes a chat that makes clips with the watch's settings) and starts the posts whose time has come."""

from __future__ import annotations

import threading
import time
import traceback

from ..download import latest_uploads, require_public_url
from ..tools import ClippyError, log
from . import results, schedule
from .jobs import english, tx
from .presets import PRESETS

TICK = 30.0
MEASURE_EVERY = 600.0
EVERY_MINUTES = (15, 30, 60, 180, 360, 1440)
DEFAULT_EVERY = 60
LIST_LIMIT = 10
NEW_PER_CHECK = 3
SEEN_KEEP = 300
PRESET_KEYS = tuple(p.key for p in PRESETS)


def watch_settings(n: int = 0, preset: str = "", instructions: str = "", every: int = DEFAULT_EVERY,
                   platforms=(), youtube_privacy: str = "public", tiktok_mode: str = "draft", slots="") -> dict:
    """A watch's clip and posting settings, checked: automatic posting needs both platforms and daily slots."""
    chosen, options = schedule.post_settings(platforms, youtube_privacy, tiktok_mode)
    times = schedule.clean_slots(slots)
    if bool(chosen) != bool(times):
        raise ClippyError(key="error.autopost_incomplete")
    return {"n": max(0, min(12, int(n or 0))), "preset": preset if preset in PRESET_KEYS else "",
            "instructions": str(instructions or "").strip()[:400],
            "every": every if every in EVERY_MINUTES else DEFAULT_EVERY,
            "platforms": chosen, "options": options, "slots": times}


def generate_payload(url: str, settings: dict) -> dict:
    """The same job a ready-made prompt starts: the preset's settings and picking rules, then the watch's own wishes."""
    preset = next((p for p in PRESETS if p.key == settings.get("preset")), None)
    options = {k: v for k, v in (preset.options if preset else {}).items() if k != "n"}
    n = settings.get("n") or (preset.options["n"] if preset else 5)
    rules = "\n".join(x for x in ((preset.instructions if preset else ""), settings.get("instructions") or "") if x)
    payload = {"url": url, "n": n, "instructions": rules, "options": options}
    if settings.get("platforms") and settings.get("slots"):
        payload["autopost"] = {k: settings[k] for k in ("platforms", "options", "slots")}
    return payload


def present_watch(w: dict) -> dict:
    problem = w.get("problem") or {}
    return {"id": w["id"], "url": w["url"], "title": w["title"], "settings": w["settings"], "enabled": bool(w["enabled"]),
            "checked": w["checked"], **({"error_t": problem} if problem.get("key") else {})}


class Watcher:
    def __init__(self, db, runner, hub, lister=None, clock=time.time):
        self.db, self.runner, self.hub, self.clock = db, runner, hub, clock
        self.lister = lister or (lambda url, limit: latest_uploads(url, limit))
        self._lock = threading.Lock()
        self._measured = 0.0

    def start(self) -> None:
        threading.Thread(target=self._loop, name="clippy-watch", daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                traceback.print_exc()
            time.sleep(TICK)

    def tick(self) -> None:
        with self._lock:
            self.post_due()
            now = self.clock()
            if now - self._measured >= MEASURE_EVERY:
                self._measured = now
                self.measure()
            for w in self.db.list_watches():
                every = 60 * int((w.get("settings") or {}).get("every") or DEFAULT_EVERY)
                if w["enabled"] and now - float(w["checked"] or 0) >= every:
                    self.check(w)

    def post_due(self) -> int:
        """Planned posts whose time has come start as posting jobs; a clip deleted since then is dropped."""
        started = 0
        for item in self.db.due_posts(self.clock()):
            alive = any(c["number"] == item["number"] for c in self.db.current_clips(item["chat_id"]))
            if not self.db.set_scheduled_state(item["id"], "started" if alive else "dropped"):
                continue
            if alive:
                self.runner.submit(item["chat_id"], "publish", {"clip": item["number"], "platforms": item["platforms"],
                                                                "options": item["settings"]})
                started += 1
            self.refresh_clip(item["chat_id"], item["number"])
        return started

    def measure(self) -> int:
        """Reads how the recent posts are doing; the open windows refresh their cards when something changed."""
        done = results.refresh(self.db, self.runner.accounts, self.clock())
        if done:
            self.hub.publish(None, {"type": "results"})
        return done

    def refresh_clip(self, chat_id: int, number: int) -> None:
        row = next((c for c in self.db.current_clips(chat_id) if c["number"] == number), None)
        if row is not None:
            self.hub.publish(chat_id, {"type": "clip", "clip": self.runner.presenter.present(row, current=True)})

    def check(self, w: dict) -> int:
        """New videos of one watched channel: the newest few become chats, the rest are only noted as seen."""
        try:
            name, uploads = self.lister(w["url"], LIST_LIMIT)
        except ClippyError as exc:
            self.db.update_watch(w["id"], checked=self.clock(), problem=tx(exc.key or "error.plain", **exc.params))
            self.changed()
            return 0
        except Exception as exc:
            log(f"[watch] {w['url']}: {type(exc).__name__}: {exc}")
            self.db.update_watch(w["id"], checked=self.clock(), problem=tx("error.watch_unreachable"))
            self.changed()
            return 0
        seen = list(w.get("seen") or [])
        new = [u for u in uploads if u.vid not in seen]
        for upload in reversed(new[:NEW_PER_CHECK]):
            self.start_chat({**w, "title": w["title"] or name}, upload)
        self.db.update_watch(w["id"], seen=([u.vid for u in new] + seen)[:SEEN_KEEP], checked=self.clock(),
                             problem={}, **({"title": name} if name and not w["title"] else {}))
        if new:
            log(f"[watch] {w['title'] or name}: {len(new)} new video(s)")
        self.changed()
        return min(len(new), NEW_PER_CHECK)

    def add(self, url: str, settings: dict, latest: bool = False) -> dict:
        """A new watch. Videos already on the channel are not clipped, only those that come later (and, when asked,
        the newest one now)."""
        url = require_public_url(url)
        name, uploads = self.lister(url, LIST_LIMIT)
        w = self.db.add_watch(url, name, settings, [u.vid for u in uploads], self.clock())
        if latest and uploads:
            self.start_chat(w, uploads[0])
        self.changed()
        return w

    def start_chat(self, w: dict, upload) -> dict | None:
        try:
            require_public_url(upload.url)
        except ClippyError:
            return None
        chat = self.db.create_chat((upload.title or "New chat")[:80])
        note = tx("watch.new_video", channel=w["title"] or w["url"], title=upload.title or upload.url)
        self.db.add_message(chat["id"], "assistant", english(note), data={"t": note})
        self.hub.publish(chat["id"], {"type": "chat", "chat": self.db.get_chat(chat["id"])})
        self.runner.submit(chat["id"], "generate", generate_payload(upload.url, w.get("settings") or {}))
        return chat

    def changed(self) -> None:
        self.hub.publish(None, {"type": "watches"})
