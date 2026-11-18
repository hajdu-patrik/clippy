"""Covers a platform turned down at posting (YouTube limits how many thumbnails a channel may set in a row): tried
again in the background, less and less often, until one goes through or the tries run out."""

from __future__ import annotations

import time
from pathlib import Path

import requests

from ..publish.registry import LABELS
from ..tools import ClippyError, log

FIRST = 15 * 60.0
LONGEST = 6 * 3600.0
TRIES = 8


def first_try(now: float) -> float:
    return now + FIRST


def next_try(now: float, tries: int) -> float:
    """0 once the tries run out: the post keeps the platform's own thumbnail."""
    return now + min(LONGEST, FIRST * 2 ** tries) if tries < TRIES else 0.0


def retry(db, accounts, now: float | None = None) -> int:
    now = now or time.time()
    due = db.covers_due(now)
    done = 0
    for platform in dict.fromkeys(p["platform"] for p in due):
        try:
            tokens = accounts.tokens(platform)
        except (ClippyError, OSError, requests.RequestException) as exc:
            log(f"[cover] {LABELS.get(platform, platform)} skipped: {exc}")
            continue
        adapter = accounts.platform(platform)
        for post in (p for p in due if p["platform"] == platform):
            image, tries = Path(post.get("jpg") or ""), int(post["cover_tries"]) + 1
            if not image.is_file():
                db.set_cover_retry(post["id"], 0.0, tries)
            elif adapter.set_cover(accounts.http, tokens, post["remote_id"], image):
                db.set_cover_retry(post["id"], 0.0, tries)
                log(f"[cover] {LABELS.get(platform, platform)} cover set: {post['url']}")
                done += 1
            else:
                db.set_cover_retry(post["id"], next_try(now, tries), tries)
    return done
