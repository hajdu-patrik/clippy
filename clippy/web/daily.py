"""Daily picks: every day at a set time the most watched moments of a game's streams (the streamers' biggest live
reactions, clipped by their viewers on Twitch and Kick) become clips, each with a time to post it for a US audience."""

from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta, timezone

from .. import categories
from ..download import require_public_url
from ..tools import ClippyError, log
from . import schedule
from .jobs import english, tx
from .watch import generate_payload

DEFAULT_N = 3
MAX_N = 5
DEFAULT_AT = "09:00"
DEFAULT_TIMES = ("19:00", "15:00", "12:00")
LEAD = 3600.0
WHOLE_MAX = categories.MAX_SECONDS
PICKS_KEEP = 60
SEEN_KEEP = 500


def clean_times(text) -> list[str]:
    """New York posting times, best first ("19:00, 15:00, 12:00"); the order is kept, duplicates dropped."""
    parts = text if isinstance(text, list) else str(text or "").replace(";", ",").replace(" ", ",").split(",")
    out = []
    for part in (str(p).strip() for p in parts):
        if not part:
            continue
        found = schedule.SLOT.match(part)
        if not found:
            raise ClippyError(key="error.bad_slot", slot=part[:12])
        slot = f"{int(found.group(1)):02d}:{found.group(2)}"
        if slot not in out:
            out.append(slot)
    if len(out) > schedule.MAX_SLOTS:
        raise ClippyError(key="error.too_many_slots", max=schedule.MAX_SLOTS)
    return out or list(DEFAULT_TIMES)


def daily_settings(n=DEFAULT_N, at=DEFAULT_AT, times="", platforms=()) -> dict:
    run = schedule.clean_slots(at or DEFAULT_AT)
    if len(run) != 1:
        raise ClippyError(key="error.bad_slot", slot=str(at)[:12])
    try:
        count = int(n or DEFAULT_N)
    except (TypeError, ValueError):
        count = DEFAULT_N
    slots = clean_times(times)
    posting = [p for p in dict.fromkeys(platforms or ()) if p in schedule.POSTING]
    return {"n": max(1, min(MAX_N, count, len(slots))), "at": run[0], "times": slots, "platforms": posting}


def _nth_sunday(year: int, month: int, nth: int) -> datetime:
    first = datetime(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (nth - 1))


def eastern_offset(ts: float) -> int:
    """Hours New York is behind UTC at `ts`: daylight time runs from the second Sunday of March 2:00 to the first
    Sunday of November 2:00 (the US rule since 2007), so no time zone database is needed."""
    utc = datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)
    start = _nth_sunday(utc.year, 3, 2) + timedelta(hours=7)
    end = _nth_sunday(utc.year, 11, 1) + timedelta(hours=6)
    return -4 if start <= utc < end else -5


def eastern_time(day: date, slot: str) -> float:
    """The moment New York's clock shows `slot` on `day`."""
    hour, minute = map(int, slot.split(":"))
    naive = datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc).timestamp()
    return naive - 3600 * eastern_offset(naive + 5 * 3600)


def eastern_clock(ts: float) -> str:
    return (datetime.fromtimestamp(ts, timezone.utc) + timedelta(hours=eastern_offset(ts))).strftime("%H:%M")


def post_times(times: list[str], count: int, now: float) -> list[float]:
    """For the best `count` times (in their order), the next moment each comes at least an hour from now."""
    today = (datetime.fromtimestamp(now, timezone.utc) + timedelta(hours=eastern_offset(now))).date()
    out = []
    for slot in times[:count]:
        for day in range(3):
            due = eastern_time(today + timedelta(days=day), slot)
            if due >= now + LEAD and due not in out:
                out.append(due)
                break
    return out


def choose(moments: list, seen: set[str], count: int, taken: set[str] = frozenset()) -> list:
    """The most viewed new moments, one per streamer (and none of the streamers in `taken`)."""
    out, streamers = [], {s.lower() for s in taken}
    for m in moments:
        if m.vid in seen or m.streamer.lower() in streamers:
            continue
        out.append(m)
        streamers.add(m.streamer.lower())
        if len(out) == count:
            break
    return out


def instructions(game: categories.Game, m) -> str:
    return (f"A {game.name} moment from the live stream of {m.streamer} on {m.platform}: keep the streamer's live "
            f"reaction, face and voice, in the clip. Credit @{m.streamer} in the description and use the hashtags "
            f"{' '.join(game.tags)}.")


def run_time(settings: dict, now: float) -> float:
    hour, minute = map(int, (settings.get("at") or DEFAULT_AT).split(":"))
    return datetime.fromtimestamp(now).replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()


class Daily:
    def __init__(self, db, runner, hub, finder=None, clock=time.time):
        self.db, self.runner, self.hub, self.clock = db, runner, hub, clock
        self.finder = finder or (lambda game, language: categories.top_moments(game, language))
        self._lock = threading.Lock()

    def tick(self) -> None:
        now = self.clock()
        for d in self.db.list_daily():
            if d["enabled"] and now >= run_time(d["settings"] or {}, now) > float(d["ran"] or 0):
                self.run(d)

    def add(self, game_key: str, settings: dict, now: bool = True) -> dict:
        """Daily picks of a game (one per game: asking again changes its settings); the first ones start now."""
        game = categories.game(game_key)
        if game is None:
            raise ClippyError(key="error.daily_unknown_game")
        found = next((d for d in self.db.list_daily() if d["game"] == game.key), None)
        d = (self.db.update_daily(found["id"], settings=settings, enabled=1) if found
             else self.db.add_daily(game.key, settings))
        if now:
            self.run(d)
        self.changed()
        return self.db.get_daily(d["id"])

    def run(self, d: dict) -> int:
        """Today's picks: the most viewed new moments of the last day, one per streamer, each its own chat. Run again
        on the same day, it only fills the places of today's picks that are gone, at their free times."""
        with self._lock:
            d = self.db.get_daily(d["id"]) or d
            game, settings, now = categories.game(d["game"]), d["settings"] or {}, self.clock()
            if game is None:
                self.db.update_daily(d["id"], ran=now, problem=tx("error.daily_unknown_game"))
                return 0
            times = settings.get("times") or list(DEFAULT_TIMES)
            seen = list(d["seen"] or [])
            today = todays(self.db, d, now)
            used = {p.get("slot") or eastern_clock(p["due"]) for p in today}
            free = [slot for slot in times if slot not in used]
            want = min(int(settings.get("n") or DEFAULT_N), len(times)) - len(today)
            picked = (choose(self.finder(game, "en"), set(seen), min(want, len(free)), {p["streamer"] for p in today})
                      if want > 0 and free else [])
            dues = post_times(free, len(picked), now)
            made = []
            for i, (m, due, slot) in enumerate(zip(picked, dues, free), len(today) + 1):
                chat = self.start_chat(game, m, due, i, len(today) + len(picked), settings.get("platforms") or [])
                if chat is not None:
                    made.append({"chat_id": chat["id"], "due": due, "slot": slot, "streamer": m.streamer,
                                 "platform": m.platform, "views": m.views, "url": m.url, "title": m.title, "run": now})
            problem = {} if made or want <= 0 else tx("error.daily_nothing", game=game.name)
            self.db.update_daily(d["id"], ran=now, problem=problem, seen=([m.vid for m in picked] + seen)[:SEEN_KEEP],
                                 picks=(made + list(d["picks"] or []))[:PICKS_KEEP])
            log(f"[daily] {game.name}: {len(made)} pick(s)")
        self.changed()
        return len(made)

    def start_chat(self, game: categories.Game, m, due: float, i: int, n: int, platforms=()) -> dict | None:
        try:
            require_public_url(m.url)
        except ClippyError:
            return None
        chat = self.db.create_chat(f"{game.name} · {m.streamer}"[:80])
        today = datetime.fromtimestamp(due).date() == datetime.fromtimestamp(self.clock()).date()
        note = tx("daily.pick_today" if today else "daily.pick_tomorrow", game=game.name, i=i, n=n,
                  streamer=m.streamer, platform=m.platform, views=m.views,
                  time=datetime.fromtimestamp(due).strftime("%H:%M"), ny=eastern_clock(due))
        self.db.add_message(chat["id"], "assistant", english(note), data={"t": note})
        self.hub.publish(chat["id"], {"type": "chat", "chat": self.db.get_chat(chat["id"])})
        payload = generate_payload(m.url, {"preset": "gameplay", "n": 1, "instructions": instructions(game, m)})
        payload["options"].update(whole=True, max_len=WHOLE_MAX)
        if platforms:
            payload["post"] = {"platforms": list(platforms), "at": due}
        self.runner.submit(chat["id"], "generate", payload)
        return chat

    def changed(self) -> None:
        self.hub.publish(None, {"type": "daily"})


def same_day(a: float, b: float) -> bool:
    return datetime.fromtimestamp(a).date() == datetime.fromtimestamp(b).date()


def todays(db, d: dict, now: float) -> list[dict]:
    """The picks made on `now`'s day whose chat is still there."""
    return [p for p in d["picks"] or [] if same_day(p["run"], now) and db.get_chat(p["chat_id"])]


def present_daily(db, presenter, d: dict) -> dict:
    """A daily pick with the clips of its newest day: the time to post, the streamer and, once made, the post text."""
    game = categories.game(d["game"])
    picks = [p for p in d["picks"] or [] if db.get_chat(p["chat_id"])]
    newest = max((p["run"] for p in picks), default=0)
    shown = []
    for p in sorted((p for p in picks if same_day(p["run"], newest)), key=lambda p: p["due"]):
        row = next(iter(db.current_clips(p["chat_id"])), None)
        clip = presenter.present(row) if row else None
        last = (db.list_messages(p["chat_id"]) or [{}])[-1]
        shown.append({**p, "number": clip["number"] if clip else None, "failed": not clip and last.get("kind") == "error",
                      "clip_title": (clip["clip"] or {}).get("title", "") if clip else "",
                      "text": clip["txt"].split("\n---\n")[0].strip() if clip else "", "ny": eastern_clock(p["due"])})
    problem = d.get("problem") or {}
    return {"id": d["id"], "game": d["game"], "name": game.name if game else d["game"], "settings": d["settings"],
            "enabled": bool(d["enabled"]), "ran": d["ran"], "picks": shown,
            **({"error_t": problem} if problem.get("key") else {})}
