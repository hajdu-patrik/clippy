"""How posted clips did: views, likes, comments, shares, how much of the clip people watched and how many were still
there after the first 3 seconds, read back from the platforms with the creator's own accounts. A post the creator made
by hand is tied to its clip by its link."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import parse_qs, urlsplit

from ..tools import ClippyError
from . import TIMEOUT, Tokens, bearer, checked
from .instagram import GRAPH as INSTAGRAM_GRAPH
from .youtube import API as YOUTUBE_API

YOUTUBE_ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"
INSTAGRAM_METRICS = ("views", "reach", "likes", "comments", "shares", "saved", "ig_reels_avg_watch_time")
BATCH = 50
HOLD_AT = 3.0
PAGES = 10
YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}")
INSTAGRAM_CODE = re.compile(r"[A-Za-z0-9_-]{5,40}")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}
INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com"}


@dataclass
class Stats:
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    watched: float | None = None
    hold: float | None = None
    curve: list | None = None
    published: float | None = field(default=None, compare=False)

    def to_json(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None and k != "published"}


def _int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _time(value) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def post_of_link(url: str) -> tuple[str, str, str]:
    """(platform, the post's id there or '' when only the platform can tell it, a clean link) of a YouTube or
    Instagram post link; anything else is refused."""
    parts = urlsplit(str(url or "").strip())
    host, path = (parts.hostname or "").lower(), [x for x in parts.path.split("/") if x]
    web = parts.scheme in ("http", "https")
    if web and (host in YOUTUBE_HOSTS or host == "youtu.be"):
        if host == "youtu.be":
            found = path[0] if path else ""
        elif path[:1] == ["watch"]:
            found = (parse_qs(parts.query).get("v") or [""])[0]
        else:
            found = path[1] if len(path) > 1 and path[0] in ("shorts", "live", "embed") else ""
        if YOUTUBE_ID.fullmatch(found):
            return "youtube", found, f"https://www.youtube.com/shorts/{found}"
    if web and host in INSTAGRAM_HOSTS:
        at = next((i for i, x in enumerate(path) if x in ("reel", "reels", "p", "tv")), None)
        found = path[at + 1] if at is not None and at <= 1 and len(path) > at + 1 else ""
        if INSTAGRAM_CODE.fullmatch(found):
            return "instagram", "", f"https://www.instagram.com/reel/{found}/"
    raise ClippyError(key="error.post_link")


def instagram_code(url: str) -> str:
    return [x for x in urlsplit(url).path.split("/") if x][-1]


def hold_at(curve: list, seconds: float, duration: float) -> float | None:
    """Viewers per view still there `seconds` into the video, read off the retention curve."""
    if not curve or duration <= 0:
        return None
    at = min(1.0, seconds / duration)
    before, after = [p for p in curve if p[0] <= at], [p for p in curve if p[0] >= at]
    if not before:
        return round(after[0][1], 3)
    if not after:
        return round(before[-1][1], 3)
    (x0, y0), (x1, y1) = before[-1], after[0]
    return round(y0 if x1 == x0 else y0 + (y1 - y0) * (at - x0) / (x1 - x0), 3)


def youtube_retention(http, tokens: Tokens, video_id: str) -> list[list[float]]:
    """Audience retention of one video: [share of the video gone by, viewers per view there], above 1 where people
    watch a part again (a Short that loops)."""
    report = checked(http.get(YOUTUBE_ANALYTICS, headers=bearer(tokens.access_token), timeout=TIMEOUT, params={
        "ids": "channel==MINE", "startDate": "2005-01-01", "endDate": dt.date.today().isoformat(),
        "metrics": "audienceWatchRatio", "dimensions": "elapsedVideoTimeRatio", "filters": f"video=={video_id}",
        "sort": "elapsedVideoTimeRatio"}), "YouTube")
    names = [h.get("name") for h in report.get("columnHeaders") or []]
    rows = [dict(zip(names, row)) for row in report.get("rows") or []]
    return sorted([round(float(r["elapsedVideoTimeRatio"]), 3), round(float(r["audienceWatchRatio"]), 3)]
                  for r in rows if r.get("elapsedVideoTimeRatio") is not None and r.get("audienceWatchRatio") is not None)


def youtube_stats(http, tokens: Tokens, ids: list[str], durations: dict[str, float] | None = None) -> dict[str, Stats]:
    """Counts and the time each video went up from the Data API; from YouTube Analytics, when the sign-in allows it,
    the share watched and, for a video of known length, its retention curve with the viewers left after 3 seconds (an
    account connected before Clippy asked for analytics has to connect again for that)."""
    out: dict[str, Stats] = {}
    for start in range(0, len(ids), BATCH):
        part = ids[start:start + BATCH]
        data = checked(http.get(f"{YOUTUBE_API}/videos", params={"part": "statistics,snippet", "id": ",".join(part)},
                                headers=bearer(tokens.access_token), timeout=TIMEOUT), "YouTube")
        for item in data.get("items") or []:
            s = item.get("statistics") or {}
            out[item["id"]] = Stats(_int(s.get("viewCount")), _int(s.get("likeCount")), _int(s.get("commentCount")),
                                    published=_time((item.get("snippet") or {}).get("publishedAt")))
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
        for video_id in part:
            length = float((durations or {}).get(video_id) or 0)
            if video_id not in out or length <= 0:
                continue
            try:
                curve = youtube_retention(http, tokens, video_id)
            except ClippyError:
                continue
            if curve:
                out[video_id].curve, out[video_id].hold = curve, hold_at(curve, HOLD_AT, length)
    return out


def instagram_media(http, tokens: Tokens, codes: set[str]) -> dict[str, tuple[str, float | None]]:
    """{shortcode: (media id, when it went up)} of the account's own posts, looked up page by page."""
    out: dict[str, tuple[str, float | None]] = {}
    url = f"{INSTAGRAM_GRAPH}/me/media"
    params: dict | None = {"fields": "id,shortcode,timestamp", "limit": 50, "access_token": tokens.access_token}
    for _ in range(PAGES):
        data = checked(http.get(url, params=params, timeout=TIMEOUT), "Instagram")
        for item in data.get("data") or []:
            if item.get("shortcode") in codes and item.get("id"):
                out[item["shortcode"]] = (str(item["id"]), _time(item.get("timestamp")))
        url, params = str((data.get("paging") or {}).get("next") or ""), None
        if codes <= out.keys() or not url.startswith(f"{INSTAGRAM_GRAPH}/"):
            break
    return out


def instagram_stats(http, tokens: Tokens, media_id: str, duration: float) -> Stats:
    """Reel insights; the share watched is the average watch time over the clip's length (the API tells no
    retention curve for a Reel)."""
    data = checked(http.get(f"{INSTAGRAM_GRAPH}/{media_id}/insights", timeout=TIMEOUT, params={
        "metric": ",".join(INSTAGRAM_METRICS), "access_token": tokens.access_token}), "Instagram")
    values = {m.get("name"): ((m.get("values") or [{}])[0]).get("value") for m in data.get("data") or []}
    average = float(values.get("ig_reels_avg_watch_time") or 0) / 1000
    return Stats(_int(values.get("views") or values.get("reach")), _int(values.get("likes")),
                 _int(values.get("comments")), _int(values.get("shares")),
                 round(min(1.0, average / duration), 3) if average and duration else None)
