"""Learning from the posted clips: how each did against the creator's own usual (the median of the same platform),
which kinds of clips do better and which openings keep viewers past the first 3 seconds. The picker gets the clear
patterns as a soft preference. A post the creator made by hand counts once its link is on the clip."""

from __future__ import annotations

import math
import statistics
import time
from collections import defaultdict
from datetime import datetime

from ..highlights import PEAK_REASON
from ..publish.registry import LABELS
from ..publish.stats import instagram_code, instagram_media, instagram_stats, youtube_stats
from ..tools import ClippyError, log

EVERY = 6 * 3600
MAX_AGE = 30 * 86400
MIN_AGE = 2 * 86400
MIN_POSTS = 5
MIN_GROUP = 2
MIN_EFFECT = 0.3
MIN_HOLD = 0.08
AGREE = 2 / 3
TOP = 8
PICKING = ("hook", "length", "kind")
HOLDING = ("hook", "kind")
LENGTHS = ((20, "0-20"), (35, "20-35"), (50, "35-50"), (10 ** 6, "50+"))
HOURS = ((6, "night"), (12, "morning"), (17, "afternoon"), (22, "evening"), (24, "night"))


def find_instagram(db, accounts) -> int:
    """Gives the Reels linked by hand their media id and the time they went up, from the account's own posts."""
    posts = db.posts_to_find("instagram")
    if not posts:
        return 0
    found = instagram_media(accounts.http, accounts.tokens("instagram"), {instagram_code(p["url"]) for p in posts})
    for p in posts:
        if instagram_code(p["url"]) in found:
            db.set_post_found(p["id"], *found[instagram_code(p["url"])])
    return sum(instagram_code(p["url"]) in found for p in posts)


def refresh(db, accounts, now: float | None = None) -> int:
    """Reads how the recent posts are doing; a platform that cannot tell (not connected, no permission) is skipped."""
    now = now or time.time()
    try:
        find_instagram(db, accounts)
    except Exception as exc:
        log(f"[results] Instagram posts not looked up: {type(exc).__name__}: {exc}")
    due = db.posts_to_measure(now, EVERY, MAX_AGE)
    done = 0
    for platform in ("youtube", "instagram"):
        posts = [p for p in due if p["platform"] == platform]
        if not posts:
            continue
        try:
            tokens = accounts.tokens(platform)
            if platform == "youtube":
                found = youtube_stats(accounts.http, tokens, [p["remote_id"] for p in posts],
                                      {p["remote_id"]: length_of(p) for p in posts})
                read = {p["id"]: found.get(p["remote_id"]) for p in posts}
                for p in posts:
                    if p["state"] == "linked" and read[p["id"]] is not None and read[p["id"]].published:
                        db.set_post_found(p["id"], p["remote_id"], read[p["id"]].published)
            else:
                read = {p["id"]: instagram_stats(accounts.http, tokens, p["remote_id"], length_of(p)) for p in posts}
        except (ClippyError, OSError) as exc:
            log(f"[results] {LABELS[platform]} skipped: {exc}")
            continue
        except Exception as exc:
            log(f"[results] {LABELS[platform]} skipped: {type(exc).__name__}: {exc}")
            continue
        for post_id, stats in read.items():
            if stats is not None:
                db.set_post_stats(post_id, stats.to_json(), now)
                done += 1
    return done


def length_of(post: dict) -> float:
    clip, info = post.get("clip") or {}, post.get("info") or {}
    return float(info.get("duration_out") or (clip.get("end", 0) - clip.get("start", 0)))


def features(post: dict) -> dict[str, str]:
    clip = post.get("clip") or {}
    hour = datetime.fromtimestamp(post["created"]).hour
    out = {"length": next(name for limit, name in LENGTHS if length_of(post) <= limit),
           "hour": next(name for limit, name in HOURS if hour < limit),
           "kind": "reaction" if str(clip.get("reason") or "").startswith(PEAK_REASON) else "speech"}
    if clip.get("hook_type"):
        out["hook"] = str(clip["hook_type"])
    return out


def holds(ripe: list[dict]) -> tuple[int, list[dict]]:
    """(clips with a 3 s hold, openings that keep clearly more or fewer viewers past 3 s than the creator's usual):
    the usual is the median of the same platform; a group needs two clips or more, most of them on the same side."""
    held = [p for p in ripe if p["stats"].get("hold") is not None]
    if len(held) < MIN_POSTS:
        return len(held), []
    usual = {platform: statistics.median([p["stats"]["hold"] for p in held if p["platform"] == platform])
             for platform in {p["platform"] for p in held}}
    groups: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for p in held:
        for dimension, value in features(p).items():
            if dimension in HOLDING:
                groups[(dimension, value)].append((p["stats"]["hold"], usual[p["platform"]]))
    out = []
    for (dimension, value), items in groups.items():
        gaps = [h - u for h, u in items]
        gap = statistics.fmean(gaps)
        agree = sum(1 for g in gaps if g * gap > 0) / len(gaps)
        if len(items) < MIN_GROUP or len(items) == len(held) or abs(gap) < MIN_HOLD or agree < AGREE:
            continue
        out.append({"dimension": dimension, "value": value, "hold": round(statistics.fmean(h for h, _ in items), 2),
                    "usual": round(statistics.fmean(u for _, u in items), 2), "count": len(items)})
    out.sort(key=lambda f: abs(f["hold"] - f["usual"]), reverse=True)
    return len(held), out[:TOP]


def insights(db, now: float | None = None) -> dict:
    """{"measured", "compared", "findings": [{dimension, value, factor, watched, count}], "held", "holds": [{dimension,
    value, hold, usual, count}]}: groups of at least two clips that did clearly better or worse than the creator's
    usual in views, and openings that clearly keep more or fewer viewers past 3 s, strongest first."""
    now = now or time.time()
    posts = [p for p in db.measured_posts() if p.get("stats")]
    ripe = [p for p in posts if now - p["created"] >= MIN_AGE]
    medians = {platform: statistics.median([p["stats"].get("views", 0) for p in ripe if p["platform"] == platform])
               for platform in {p["platform"] for p in ripe}}
    groups: dict[tuple[str, str], list[tuple[float, float | None]]] = defaultdict(list)
    for p in ripe:
        effect = math.log2((p["stats"].get("views", 0) + 1) / (medians[p["platform"]] + 1))
        for dimension, value in features(p).items():
            groups[(dimension, value)].append((effect, p["stats"].get("watched")))
    findings = []
    if len(ripe) >= MIN_POSTS:
        for (dimension, value), items in groups.items():
            mean = statistics.fmean(e for e, _ in items)
            if len(items) < MIN_GROUP or abs(mean) < MIN_EFFECT or len(items) == len(ripe):
                continue
            watched = [w for _, w in items if w is not None]
            findings.append({"dimension": dimension, "value": value, "factor": round(2 ** mean, 2),
                             "watched": round(statistics.fmean(watched), 2) if watched else None,
                             "count": len(items)})
    findings.sort(key=lambda f: abs(math.log2(f["factor"])), reverse=True)
    held, kept = holds(ripe)
    return {"measured": len(posts), "compared": len(ripe), "findings": findings[:TOP], "held": held, "holds": kept}


def lessons(found: dict, dimensions: tuple[str, ...] = PICKING) -> str:
    """The findings in plain words for the LLM (English); empty while there is too little to learn from."""
    def what(f: dict) -> str:
        return {"hook": f"hook type '{f['value']}'", "length": f"clips of {f['value']} s",
                "kind": "moments picked by reactions" if f["value"] == "reaction" else "moments picked by what is said",
                "hour": f"posts in the {f['value']}"}[f["dimension"]]

    lines = []
    for f in found.get("findings") or []:
        if f["dimension"] in dimensions:
            watched = f", {round(100 * f['watched'])}% watched" if f.get("watched") is not None else ""
            lines.append(f"- {what(f)}: {f['factor']}x the usual views{watched} ({f['count']} clips)")
    for f in found.get("holds") or []:
        if f["dimension"] in dimensions:
            lines.append(f"- {what(f)}: {round(100 * f['hold'])} of 100 viewers still watching after the first 3 s, "
                         f"against the usual {round(100 * f['usual'])} ({f['count']} clips)")
    return "\n".join(lines)
