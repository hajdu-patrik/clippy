"""Planned posts: a clip goes out at a set time, or into the next free daily slot of a watched channel."""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

from ..publish import PLATFORMS
from ..tools import ClippyError

SLOT = re.compile(r"^([01]?\d|2[0-3])[:.]([0-5]\d)$")
MAX_SLOTS = 8
LEAD = 300.0
DAYS_AHEAD = 60
MIN_AHEAD = 60.0


def clean_slots(text) -> list[str]:
    """Daily times like "18:00, 21:30" (also 18.00), sorted, at most 8."""
    parts = text if isinstance(text, list) else re.split(r"[,;\s]+", str(text or ""))
    out = set()
    for part in (str(p).strip() for p in parts):
        if not part:
            continue
        found = SLOT.match(part)
        if not found:
            raise ClippyError(key="error.bad_slot", slot=part[:12])
        out.add(f"{int(found.group(1)):02d}:{found.group(2)}")
    if len(out) > MAX_SLOTS:
        raise ClippyError(key="error.too_many_slots", max=MAX_SLOTS)
    return sorted(out)


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
    chosen = [p for p in dict.fromkeys(platforms or []) if p in PLATFORMS]
    return chosen, {"youtube": {"privacy": youtube_privacy}, "tiktok": {"mode": tiktok_mode}}


def plan(db, chat_id: int, number: int, platforms: list[str], options: dict, due: float,
         now: float | None = None) -> dict:
    if due < (now or time.time()) + MIN_AHEAD:
        raise ClippyError(key="error.schedule_past")
    return db.add_scheduled(chat_id, number, platforms, options, due)


def plan_in_slots(db, chat_id: int, numbers: list[int], autopost: dict, now: float | None = None) -> list[dict]:
    """New clips of a watched channel: one per free slot, the best (lowest number) first."""
    now = now or time.time()
    taken = {round(item["due"], 3) for item in db.planned()}
    dues = next_slots(autopost.get("slots") or [], len(numbers), taken, now)
    return [db.add_scheduled(chat_id, number, autopost["platforms"], autopost.get("options") or {}, due)
            for number, due in zip(sorted(numbers), dues)]


def present(item: dict) -> dict:
    return {"id": item["id"], "due": item["due"], "platforms": item["platforms"]}
