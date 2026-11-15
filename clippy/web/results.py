"""Learning from the posted clips: how each did against the creator's own usual (the median of the same platform),
and which kinds of clips do better. The picker gets the clear patterns as a soft preference."""

from __future__ import annotations

import math
import statistics
import time
from collections import defaultdict
from datetime import datetime

from ..highlights import PEAK_REASON
from ..publish import LABELS
from ..publish.stats import instagram_stats, youtube_stats
from ..tools import ClippyError, log

EVERY = 6 * 3600
MAX_AGE = 30 * 86400
MIN_AGE = 2 * 86400
MIN_POSTS = 5
MIN_GROUP = 2
MIN_EFFECT = 0.3
TOP = 8
PICKING = ("hook", "length", "kind")
LENGTHS = ((20, "0-20"), (35, "20-35"), (50, "35-50"), (10 ** 6, "50+"))
HOURS = ((6, "night"), (12, "morning"), (17, "afternoon"), (22, "evening"), (24, "night"))


def refresh(db, accounts, now: float | None = None) -> int:
    """Reads how the recent posts are doing; a platform that cannot tell (not connected, no permission) is skipped."""
    now = now or time.time()
    due = db.posts_to_measure(now, EVERY, MAX_AGE)
    done = 0
    for platform in ("youtube", "instagram"):
        posts = [p for p in due if p["platform"] == platform]
        if not posts:
            continue
        try:
            tokens = accounts.tokens(platform)
            if platform == "youtube":
                found = youtube_stats(accounts.http, tokens, [p["remote_id"] for p in posts])
                read = {p["id"]: found.get(p["remote_id"]) for p in posts}
            else:
                read = {p["id"]: instagram_stats(accounts.http, tokens, p["remote_id"],
                                                 float((p.get("info") or {}).get("duration_out") or 0)) for p in posts}
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


def features(post: dict) -> dict[str, str]:
    clip, info = post.get("clip") or {}, post.get("info") or {}
    length = float(info.get("duration_out") or (clip.get("end", 0) - clip.get("start", 0)))
    hour = datetime.fromtimestamp(post["created"]).hour
    out = {"length": next(name for limit, name in LENGTHS if length <= limit),
           "hour": next(name for limit, name in HOURS if hour < limit),
           "kind": "reaction" if str(clip.get("reason") or "").startswith(PEAK_REASON) else "speech"}
    if clip.get("hook_type"):
        out["hook"] = str(clip["hook_type"])
    return out


def insights(db, now: float | None = None) -> dict:
    """{"measured", "compared", "findings": [{dimension, value, factor, watched, count}]}: a group of at least two
    clips that did clearly better or worse than the creator's usual, strongest first."""
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
    return {"measured": len(posts), "compared": len(ripe), "findings": findings[:TOP]}


def lessons(found: dict, dimensions: tuple[str, ...] = PICKING) -> str:
    """The findings in plain words for the LLM (English); empty while there is too little to learn from."""
    lines = []
    for f in found.get("findings") or []:
        if f["dimension"] not in dimensions:
            continue
        what = {"hook": f"hook type '{f['value']}'", "length": f"clips of {f['value']} s",
                "kind": "moments picked by reactions" if f["value"] == "reaction" else "moments picked by what is said",
                "hour": f"posts in the {f['value']}"}[f["dimension"]]
        watched = f", {round(100 * f['watched'])}% watched" if f.get("watched") is not None else ""
        lines.append(f"- {what}: {f['factor']}x the usual views{watched} ({f['count']} clips)")
    return "\n".join(lines)
