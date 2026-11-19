"""The Scheduler's agenda: every post that waits for its time, with the clip it is about, and the clips to schedule."""

from __future__ import annotations

from urllib.parse import quote

from . import schedule

RECENT = 7 * 86400.0
FREE_SLOTS = 8
STUDIO = "https://studio.youtube.com/video/{}/edit"


def saved_slots(db, saved: list[str]) -> list[str]:
    """The daily posting times: the ones the user saved, else those of the watched channels."""
    if saved:
        return schedule.clean_slots(saved)
    return sorted({slot for w in db.list_watches() for slot in (w["settings"] or {}).get("slots") or []})


def waiting_uploads(jobs) -> list[tuple]:
    """(job, platform, time) of timed uploads that are still on their way to the platform."""
    return [(job, name, at) for job in jobs for name in job.payload.get("platforms") or []
            if name not in (job.payload.get("done") or {}) and name not in (job.payload.get("failed") or {})
            and (at := ((job.payload.get("options") or {}).get(name) or {}).get("publish_at"))]


def entries(db, media, jobs, now: float) -> list[dict]:
    """Planned posts, timed uploads and timed videos, soonest first; the ones without a known time come last."""
    titles: dict[int, str] = {}

    def make(source: str, state: str, due, chat_id: int, number: int, clip: dict | None, jpg, platforms, **more) -> dict:
        if chat_id not in titles:
            titles[chat_id] = (db.get_chat(chat_id) or {}).get("title", "")
        return {"source": source, "state": state, "due": due or None, "chat_id": chat_id, "chat": titles[chat_id],
                "number": number, "title": (clip or {}).get("title") or "", "cover": media.url_for(chat_id, jpg),
                "platforms": list(platforms), **more}

    out = []
    for item in db.planned():
        row = db.current_clip(item["chat_id"], item["number"])
        if row is not None:
            out.append(make("plan", "planned", item["due"], item["chat_id"], item["number"], row["clip"], row["jpg"],
                            item["platforms"], id=item["id"]))
    for job, name, at in waiting_uploads(jobs):
        row = db.current_clip(job.chat_id, int(job.payload.get("clip") or 0))
        if row is not None:
            out.append(make("platform", "uploading", at, job.chat_id, row["number"], row["clip"], row["jpg"], [name]))
    for post in db.timed_posts(now - RECENT):
        state = "kept" if not post["publish_at"] else "scheduled" if post["publish_at"] > now else "live"
        out.append(make("platform", state, post["publish_at"], post["chat_id"], post["number"], post["clip"],
                        post["jpg"], [post["platform"]], id=post["id"], url=post["url"],
                        studio=STUDIO.format(quote(post["remote_id"], safe=""))))
    return sorted(out, key=lambda e: (e["due"] is None, e["due"] or 0))


def choices(db, media) -> list[dict]:
    """Chats with finished clips, and those clips: what the Scheduler can schedule."""
    out = []
    for chat in db.list_chats():
        clips = [{"number": c["number"], "title": (c["clip"] or {}).get("title") or "",
                  "cover": media.url_for(chat["id"], c.get("jpg"))} for c in db.current_clips(chat["id"]) if c.get("mp4")]
        if clips:
            out.append({"id": chat["id"], "title": chat["title"], "clips": clips})
    return out


def view(db, media, jobs, saved: list[str], now: float) -> dict:
    slots = saved_slots(db, saved)
    return {"items": entries(db, media, jobs, now), "choices": choices(db, media), "slots": slots,
            "saved": bool(saved), "free": schedule.next_slots(slots, FREE_SLOTS, schedule.taken_times(db, now), now)}
