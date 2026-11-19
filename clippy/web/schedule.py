"""Planned posts: a clip goes out at a set time, or into the next free daily slot of a watched channel. A platform
that schedules by itself (YouTube) gets the clip at once with its time on it, so it goes out even while this
computer is off; the others wait in Clippy's own plan."""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

from ..publish.registry import POSTING, SELF_SCHEDULING
from ..tools import ClippyError

SLOT = re.compile(r"^([01]?\d|2[0-3])[:.]([0-5]\d)$")
MAX_SLOTS = 8
LEAD = 300.0
DAYS_AHEAD = 60
MIN_AHEAD = 60.0


def parse_slots(text) -> list[str]:
    """Times of day like "18:00, 21:30" (also 18.00) as HH:MM, in the order given, each once, at most 8."""
    parts = text if isinstance(text, list) else re.split(r"[,;\s]+", str(text or ""))
    out: list[str] = []
    for part in (str(p).strip() for p in parts):
        if not part:
            continue
        found = SLOT.match(part)
        if not found:
            raise ClippyError(key="error.bad_slot", slot=part[:12])
        slot = f"{int(found.group(1)):02d}:{found.group(2)}"
        if slot not in out:
            out.append(slot)
    if len(out) > MAX_SLOTS:
        raise ClippyError(key="error.too_many_slots", max=MAX_SLOTS)
    return out


def clean_slots(text) -> list[str]:
    """Daily posting times, sorted."""
    return sorted(parse_slots(text))


def next_slots(slots: list[str], count: int, taken: set[float], now: float) -> list[float]:
    """The next `count` free slot times (this computer's local time), at least five minutes from now."""
    if not slots:
        return []
    start = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for day in range(DAYS_AHEAD):
        for slot in slots:
            hour, minute = map(int, slot.split(":"))
            due = (start + timedelta(days=day)).replace(hour=hour, minute=minute).timestamp()
            if due >= now + LEAD and due not in taken:
                out.append(due)
                taken.add(due)
                if len(out) == count:
                    return out
    return out


def post_settings(platforms, youtube_privacy: str = "public", tiktok_mode: str = "draft") -> tuple[list[str], dict]:
    chosen = [p for p in dict.fromkeys(platforms or []) if p in POSTING]
    return chosen, {"youtube": {"privacy": youtube_privacy}, "tiktok": {"mode": tiktok_mode}}


def plan(db, chat_id: int, number: int, platforms: list[str], options: dict, due: float,
         now: float | None = None) -> dict:
    if due < (now or time.time()) + MIN_AHEAD:
        raise ClippyError(key="error.schedule_past")
    return db.add_scheduled(chat_id, number, platforms, options, due)


def replan(db, item_id: int, due: float, now: float | None = None) -> dict | None:
    """A new time for a post waiting in Clippy's plan; None when it is not planned (any more)."""
    if due < (now or time.time()) + MIN_AHEAD:
        raise ClippyError(key="error.schedule_past")
    return db.get_scheduled(item_id) if db.reschedule(item_id, due) else None


def taken_times(db, now: float) -> set[float]:
    """The slot times that already hold a post: planned here, or timed on a platform that keeps them."""
    dues = [item["due"] for item in db.planned()] + [p["publish_at"] for p in db.timed_posts(now) if p["publish_at"]]
    return {float(int(due // 60) * 60) for due in dues}


def post_at(db, runner, chat_id: int, number: int, platforms: list[str], options: dict, at: float,
            now: float | None = None) -> dict:
    """One clip at a set time on each platform: uploaded now with its time where the platform keeps it, planned here
    for the rest. Returns the started job's and the planned item's ids."""
    if at < (now or time.time()) + MIN_AHEAD:
        raise ClippyError(key="error.schedule_past")
    own = [p for p in platforms if p in SELF_SCHEDULING]
    rest = [p for p in platforms if p not in own]
    out = {}
    if own:
        timed = {**options, **{p: {**(options.get(p) or {}), "publish_at": at} for p in own}}
        out["job"] = runner.submit(chat_id, "publish", {"clip": number, "platforms": own, "options": timed}).id
    if rest:
        out["scheduled"] = plan(db, chat_id, number, rest, options, at, now)["id"]
    return out


def plan_in_slots(db, chat_id: int, numbers: list[int], autopost: dict, now: float | None = None) -> list[dict]:
    """New clips of a watched channel: one per free slot, the best (lowest number) first."""
    now = now or time.time()
    dues = next_slots(autopost.get("slots") or [], len(numbers), taken_times(db, now), now)
    return [db.add_scheduled(chat_id, number, autopost["platforms"], autopost.get("options") or {}, due)
            for number, due in zip(sorted(numbers), dues)]


def present(item: dict) -> dict:
    return {"id": item["id"], "due": item["due"], "platforms": item["platforms"]}
