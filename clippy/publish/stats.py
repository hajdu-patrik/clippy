"""How posted clips did: views, likes, comments, shares and how much of the clip people watched, read back from the
platforms with the same accounts that posted them."""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass

from ..tools import ClippyError
from . import TIMEOUT, Tokens, bearer, checked

YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
YOUTUBE_ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"
INSTAGRAM_GRAPH = "https://graph.instagram.com/v26.0"
INSTAGRAM_METRICS = ("views", "reach", "likes", "comments", "shares", "saved", "ig_reels_avg_watch_time")
BATCH = 50


@dataclass
class Stats:
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    watched: float | None = None

    def to_json(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


def _int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def youtube_stats(http, tokens: Tokens, ids: list[str]) -> dict[str, Stats]:
    """Counts from the Data API; the share watched from YouTube Analytics when the sign-in allows it (an account
    connected before Clippy asked for analytics has to connect again for that)."""
    out: dict[str, Stats] = {}
    for start in range(0, len(ids), BATCH):
        part = ids[start:start + BATCH]
        data = checked(http.get(f"{YOUTUBE_API}/videos", params={"part": "statistics", "id": ",".join(part)},
                                headers=bearer(tokens.access_token), timeout=TIMEOUT), "YouTube")
        for item in data.get("items") or []:
            s = item.get("statistics") or {}
            out[item["id"]] = Stats(_int(s.get("viewCount")), _int(s.get("likeCount")), _int(s.get("commentCount")))
        try:
            report = checked(http.get(YOUTUBE_ANALYTICS, headers=bearer(tokens.access_token), timeout=TIMEOUT, params={
                "ids": "channel==MINE", "startDate": "2005-01-01", "endDate": dt.date.today().isoformat(),
                "metrics": "averageViewPercentage,shares", "dimensions": "video", "filters": f"video=={','.join(part)}"}),
                "YouTube")
        except ClippyError:
            continue
        names = [h.get("name") for h in report.get("columnHeaders") or []]
        for row in report.get("rows") or []:
            values = dict(zip(names, row))
            found = out.get(str(values.get("video")))
            if found is not None:
                found.watched = round(min(1.0, float(values.get("averageViewPercentage") or 0) / 100), 3)
                found.shares = _int(values.get("shares"))
    return out


def instagram_stats(http, tokens: Tokens, media_id: str, duration: float) -> Stats:
    """Reel insights; the share watched is the average watch time over the clip's length."""
    data = checked(http.get(f"{INSTAGRAM_GRAPH}/{media_id}/insights", timeout=TIMEOUT, params={
        "metric": ",".join(INSTAGRAM_METRICS), "access_token": tokens.access_token}), "Instagram")
    values = {m.get("name"): ((m.get("values") or [{}])[0]).get("value") for m in data.get("data") or []}
    average = float(values.get("ig_reels_avg_watch_time") or 0) / 1000
    return Stats(_int(values.get("views") or values.get("reach")), _int(values.get("likes")),
                 _int(values.get("comments")), _int(values.get("shares")),
                 round(min(1.0, average / duration), 3) if average and duration else None)
