"""Source resolution: a local file is used as-is, a URL is downloaded once with yt-dlp."""

from __future__ import annotations

import gc
import glob
import ipaddress
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .concurrency import SOURCE
from .tools import ROOT, ClippyError, ffmpeg_path, log, move_atomic, read_json, report, slugify, write_json

URL_RE = re.compile(r"^https?://", re.I)
VIDEO_EXTS = (".mp4", ".mkv", ".webm")


def is_url(source: str) -> bool:
    return bool(URL_RE.match(source.strip()))


def require_public_url(url: str) -> str:
    """Reject anything but http(s) to a public address, so a chat message cannot reach local services (SSRF)."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ClippyError(key="error.only_http")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ClippyError(key="error.unresolvable", host=parts.hostname) from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ClippyError(key="error.not_public", host=parts.hostname)
    return url.strip()


CHUNK_BYTES = 10 * 2 ** 20
FORBIDDEN_RETRIES = 3
RETRY_PAUSE = 2.0


def _video_id(info: dict) -> str:
    raw = str(info.get("id") or "")
    return raw if re.fullmatch(r"[\w-]{1,64}", raw) else slugify(raw or str(info.get("title") or "video"), 64)


LANDSCAPE = "[format_id!*=portrait]"
SOCKET_TIMEOUT = 60
UNNEEDED_WARNINGS = ("ffprobe not found",)


class YdlLog:
    """yt-dlp's own messages as before (errors and warnings on stderr), minus the warning that the bundled ffmpeg has
    no ffprobe: Clippy reads the file's details with ffmpeg itself."""

    def debug(self, msg: str) -> None:
        pass

    info = debug

    def warning(self, msg: str) -> None:
        if not any(w in msg for w in UNNEEDED_WARNINGS):
            print(f"WARNING: {msg}", file=sys.stderr)

    def error(self, msg: str) -> None:
        print(msg, file=sys.stderr)


def _ydl_options(target: Path, max_height: int, cookies_from_browser: str | None, vid: str = "%(id)s") -> dict:
    opts = {
        "format": f"bv*[height<={max_height}]{LANDSCAPE}+ba/b[height<={max_height}]{LANDSCAPE}/bv*[height<={max_height}]+ba/"
                  f"b[height<={max_height}]/b",
        "format_sort": ["hdr:sdr", f"res:{max_height}", "fps", "vcodec:vp9", "acodec:opus"],
        "merge_output_format": "mkv",
        "outtmpl": str(target / f"{vid}.%(ext)s"),
        "ffmpeg_location": ffmpeg_path(),
        "noplaylist": True,
        "playlist_items": "1:10",
        "quiet": True,
        "noprogress": True,
        "http_chunk_size": CHUNK_BYTES,
        "socket_timeout": SOCKET_TIMEOUT,
        "retries": 10,
        "extractor_retries": 5,
        "logger": YdlLog(),
    }
    runtimes = {name: {} for name in ("deno", "node") if shutil.which(name)}
    if runtimes:
        opts["js_runtimes"] = runtimes
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    return opts


def _progress_hook(parts: int):
    finished: set[str] = set()

    def hook(d: dict) -> None:
        name = str(d.get("filename") or "")
        if d.get("status") == "finished":
            finished.add(name)
            report(len(finished) / parts)
        elif d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            part = (d.get("downloaded_bytes") or 0) / total if total else 0.0
            report((len(finished - {name}) + part) / parts)
    return hook


def _downloaded(folder: Path, vid: str) -> Path | None:
    """Only the finished, merged file counts (`<id>.<ext>`), never a format fragment like `<id>.f137.mp4`."""
    return next((folder / f"{vid}{ext}" for ext in VIDEO_EXTS if (folder / f"{vid}{ext}").is_file()), None)


@dataclass(frozen=True)
class Remote:
    """A video page resolved by yt-dlp: its stable id (the cache key) and title."""

    url: str
    info: dict
    vid: str
    title: str
    single: bool = True
    item: int | None = None

    @property
    def sound_track(self) -> str | None:
        """The format id of the video's own sound when the site serves it as a separate stream (YouTube, Twitch
        VODs): it can be fetched first. A site's other audio-only formats may be something else entirely (TikTok's
        "audio" is the song the video uses), so only the stream chosen for this very video counts."""
        if not self.single:
            return None
        return next((f.get("format_id") for f in self.info.get("requested_formats") or []
                     if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")), None)

    @property
    def streamed(self) -> bool:
        """Served as HLS fragments (Kick, Twitch): those can be byte ranges themselves, which chunked requests break."""
        formats = self.info.get("requested_formats") or [self.info]
        return any(str(f.get("protocol") or "").startswith("m3u8") for f in formats)

    @property
    def duration(self) -> float | None:
        return float(self.info["duration"]) if self.info.get("duration") else None

    @property
    def is_stream(self) -> bool:
        """A recorded live stream: a Twitch or Kick VOD, or a YouTube video that was live."""
        kind = str(self.info.get("extractor_key") or self.info.get("extractor") or "").lower()
        return self.info.get("live_status") == "was_live" or kind.startswith(("twitch", "kick"))

    @property
    def has_chat(self) -> bool:
        """A stream replay whose chat the site keeps (YouTube lives); Twitch VOD chat is not served to downloaders."""
        return (self.single and self.info.get("live_status") not in ("is_live", "is_upcoming")
                and bool((self.info.get("subtitles") or {}).get("live_chat")))


ANSI = re.compile(r"\x1b\[[0-9;]*m")
REASONS = (
    ("error.download_login", ("login", "log in", "logged-in", "logged in", "sign in", "sign-in", "authentication",
                              "cookies", "private video", "members-only", "age-restricted", "confirm your age")),
    ("error.download_busy", ("429", "too many requests", "rate-limit", "rate limit")),
    ("error.download_gone", ("unavailable", "not available", "not found", "does not exist", "removed", "deleted",
                             "404", "copyright", "blocked")),
    ("error.download_unsupported", ("unsupported url", "no video formats", "no video could be found",
                                    "there's no video", "no video in")),
)
UNREADABLE_COOKIES = ("cookies database", "cookie database", "failed to decrypt", "could not copy", "keyring")
LIST_EXTRACTORS = ("tab", "playlist", "user", "channel", "profile", "search", "feed", "collection")
LIST_PATH = re.compile(r"^/(@[^/]+/?$|channel/|c/|user/|playlist|feed/)")
PROFILE_HOSTS = ("instagram.com", "twitch.tv", "kick.com", "x.com", "twitter.com", "facebook.com")


def _looks_like_a_list(host: str, path: str) -> bool:
    """A profile, channel or playlist link: `/@name`, `/channel/…`, or just `/name` on a site where that is a profile."""
    if LIST_PATH.match(path):
        return True
    site = host.removeprefix("www.").removeprefix("m.")
    return site in PROFILE_HOSTS and re.fullmatch(r"/[^/]+/?", path) is not None


def explain(message: str, host: str, path: str = "") -> ClippyError:
    """A yt-dlp failure as a readable error in the user's language: why it failed and what helps. `path`: the
    link's path, so a profile or channel link that fails to resolve is still named as one."""
    detail = ANSI.sub("", message)
    detail = re.sub(r"^ERROR:\s*", "", detail).split("; please report", 1)[0].strip()
    lower = detail.lower()
    key = next((key for key, words in REASONS if any(w in lower for w in words)), "error.download_failed")
    if key == "error.download_failed" and _looks_like_a_list(host, path):
        key = "error.download_playlist"
    return ClippyError(key=key, host=host, detail=detail[:200])


def _single(info: dict, host: str) -> tuple[dict, bool, int | None]:
    """A post with several items (an Instagram carousel) gives its first video; a profile, channel or playlist is
    refused, a live stream too (it has no end yet)."""
    if info.get("live_status") in ("is_live", "is_upcoming", "post_live") or info.get("is_live"):
        raise ClippyError(key="error.download_live", host=host)
    if info.get("_type") != "playlist" and not info.get("entries"):
        return info, True, None
    kind = str(info.get("extractor_key") or info.get("ie_key") or "").lower()
    if any(word in kind for word in LIST_EXTRACTORS):
        raise ClippyError(key="error.download_playlist", host=host)
    item = next((i for i, e in enumerate(info.get("entries") or [], 1) if e and any(
        f.get("vcodec") not in (None, "none") for f in e.get("formats") or [e])), None)
    if item is None:
        raise ClippyError(key="error.download_unsupported", host=host, detail="")
    return _single(list(info["entries"])[item - 1], host)[0], False, item


def cookies_file() -> Path | None:
    """CLIPPY_COOKIES_FILE: a cookies.txt (Netscape format) exported from a signed-in browser; relative to the
    project folder."""
    raw = (os.environ.get("CLIPPY_COOKIES_FILE") or "").strip().strip('"')
    if not raw:
        return None
    path = ROOT / Path(raw).expanduser()
    if not path.is_file():
        raise ClippyError(key="error.cookies_file_missing", path=path.name)
    return path


@contextmanager
def _session(opts: dict):
    """A YoutubeDL with its own copy of the cookies file: yt-dlp writes the cookies back when it closes, and parallel
    jobs must neither race on the user's file nor change it."""
    source = cookies_file()
    if source is None:
        with _yt_dlp().YoutubeDL(opts) as ydl:
            yield ydl
        return
    with tempfile.TemporaryDirectory(prefix="clippy-cookies-") as private:
        copy = Path(private) / "cookies.txt"
        shutil.copyfile(source, copy)
        with _yt_dlp().YoutubeDL({**opts, "cookiefile": str(copy)}) as ydl:
            yield ydl


def _extract(url: str, opts: dict, download: bool = False) -> dict:
    """Runs yt-dlp on a link. When the browser named for cookies cannot give them (not installed, another profile,
    open and locking them, encrypted), a public video is not held back: it is tried again without them, and one that
    needs a sign-in says that the browser's cookies could not be read."""
    try:
        with _session(opts) as ydl:
            return ydl.extract_info(url, download=download)
    except _yt_dlp().utils.DownloadError as exc:
        browser = (opts.get("cookiesfrombrowser") or (None,))[0]
        if not browser or not any(w in str(exc).lower() for w in UNREADABLE_COOKIES):
            raise
    log(f"[download] the cookies of {browser} cannot be read; trying without them")
    try:
        with _session({k: v for k, v in opts.items() if k != "cookiesfrombrowser"}) as ydl:
            return ydl.extract_info(url, download=download)
    except _yt_dlp().utils.DownloadError as exc:
        parts = urlsplit(url)
        if explain(str(exc), parts.hostname or url, parts.path).key == "error.download_login":
            raise ClippyError(key="error.cookies_unreadable", host=parts.hostname or url, browser=browser) from None
        raise


def _yt_dlp():
    try:
        import yt_dlp
    except ImportError as exc:
        raise ClippyError('yt-dlp is missing: pip install "yt-dlp[default]"') from exc
    return yt_dlp


@contextmanager
def _site_errors(url: str):
    """yt-dlp failures become a readable error in the user's language instead of a traceback."""
    yt_dlp = _yt_dlp()
    try:
        yield
    except yt_dlp.utils.DownloadError as exc:
        parts = urlsplit(url)
        raise explain(str(exc), parts.hostname or url, parts.path) from None


def probe(source: str, downloads: Path, max_height: int = 2160, cookies_from_browser: str | None = None) -> Remote:
    downloads.mkdir(parents=True, exist_ok=True)
    with _site_errors(source):
        info = _extract(source, _ydl_options(downloads, max_height, cookies_from_browser))
    info, single, item = _single(info, urlsplit(source).hostname or source)
    vid = _video_id(info)
    return Remote(source, info, vid, info.get("title") or vid, single, item)


def cached_video(downloads: Path, vid: str) -> Path | None:
    return _downloaded(downloads, vid)


PARTIAL_MAX_AGE = 6 * 3600


def remove_partial(folder: Path, tries: int = 6) -> None:
    """A download's private folder goes. A stopped download can still hold its file open on Windows until its thread
    has ended, so a folder that stays is tried again a little later."""
    shutil.rmtree(folder, ignore_errors=True)
    if folder.exists() and tries:
        def again() -> None:
            gc.collect()
            remove_partial(folder, tries - 1)
        timer = threading.Timer(5.0, again)
        timer.daemon = True
        timer.start()


def sweep_partials(downloads: Path, now: float | None = None) -> int:
    """Private folders of downloads that never finished (a stopped job, a crash), left for hours: removed."""
    now = now or time.time()
    gone = 0
    for folder in downloads.glob(".part-*"):
        if folder.is_dir() and now - folder.stat().st_mtime > PARTIAL_MAX_AGE:
            remove_partial(folder)
            gone += not folder.exists()
    return gone


def _download(remote: Remote, downloads: Path, name: str, opts: dict, parts: int) -> Path:
    """A refused media link (HTTP 403, YouTube does it now and then mid-download) is asked for again: every attempt
    resolves the page anew, so it gets fresh links."""
    with _site_errors(remote.url):
        for attempt in range(1, FORBIDDEN_RETRIES + 2):
            try:
                return _download_once(remote, downloads, name, opts, parts)
            except _yt_dlp().utils.DownloadError as exc:
                if "403" not in str(exc) or attempt > FORBIDDEN_RETRIES:
                    raise
                log(f"[download] the site refused a link (HTTP 403); new links, attempt {attempt + 1}")
                time.sleep(RETRY_PAUSE * attempt)
    raise ClippyError(key="error.download_missing")


def _download_once(remote: Remote, downloads: Path, name: str, opts: dict, parts: int) -> Path:
    """yt-dlp works in a private folder; only the finished file is renamed into the cache (all or nothing). The link
    is resolved again in the same session: TikTok and others tie their media URLs to that session's cookies."""
    partial = downloads / f".part-{remote.vid}-{uuid.uuid4().hex[:8]}"
    partial.mkdir()
    try:
        opts = {**opts, "outtmpl": str(partial / f"{name}.%(ext)s"), "progress_hooks": [_progress_hook(parts)],
                **({"playlist_items": str(remote.item)} if remote.item else {})}
        if remote.streamed:
            opts.pop("http_chunk_size", None)
        _extract(remote.url, opts, download=True)
        done = next((f for f in sorted(partial.iterdir()) if f.is_file() and f.name.startswith(f"{name}.")
                     and f.suffix != ".part"), None)
        if done is None:
            raise ClippyError(key="error.download_missing")
        return move_atomic(done, downloads / done.name)
    finally:
        remove_partial(partial)


def fetch_video(remote: Remote, downloads: Path, max_height: int = 2160, cookies_from_browser: str | None = None) -> Path:
    with SOURCE.hold(f"video:{remote.vid}"):
        if (cached := _downloaded(downloads, remote.vid)) is not None:
            return cached
        log(f"[download] {remote.title} ({int(remote.info.get('duration') or 0) // 60} min)")
        opts = _ydl_options(downloads, max_height, cookies_from_browser, remote.vid)
        parts = len(remote.info.get("requested_formats") or [remote.info])
        return _download(remote, downloads, remote.vid, opts, parts)


def fetch_audio(remote: Remote, downloads: Path, cookies_from_browser: str | None = None) -> Path:
    """The sound alone (a few MB): voice isolation and the transcript can start while the video still downloads."""
    name = f"{remote.vid}.audio"
    with SOURCE.hold(f"audio:{remote.vid}"):
        if (cached := next(downloads.glob(f"{glob.escape(name)}.*"), None)) is not None:
            return cached
        log(f"[download] sound first: {remote.title}")
        opts = {**_ydl_options(downloads, 2160, cookies_from_browser, name), "format": remote.sound_track}
        return _download(remote, downloads, name, opts, 1)


YOUTUBE_CHANNEL = re.compile(r"^/(@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)/?$")
TWITCH_CHANNEL = re.compile(r"^/([A-Za-z0-9_]{3,25})/?$")
NOT_FINISHED = ("is_live", "is_upcoming", "post_live")


@dataclass(frozen=True)
class Upload:
    vid: str
    url: str
    title: str
    when: float | None = None


def channel_lists(url: str) -> list[str]:
    """The pages that list a channel's finished videos: a YouTube channel's uploads and past streams, a Twitch
    channel's VODs; a playlist or a channel tab as given."""
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.").removeprefix("m.")
    base = f"{parts.scheme}://{parts.hostname}{parts.path.rstrip('/')}"
    if host == "youtube.com" and YOUTUBE_CHANNEL.match(parts.path):
        return [f"{base}/videos", f"{base}/streams"]
    if host == "twitch.tv" and TWITCH_CHANNEL.match(parts.path):
        return [f"{base}/videos"]
    return [url]


def is_list_url(url: str) -> bool:
    """A channel, a channel's tab or a playlist rather than one video."""
    parts = urlsplit(url)
    return (channel_lists(url) != [url] or "list=" in parts.query
            or bool(re.search(r"/(videos|streams|playlist|shorts)/?$", parts.path)))


def _listing_options(limit: int, cookies_from_browser: str | None = None) -> dict:
    opts = {**_ydl_options(Path(tempfile.gettempdir()), 2160, cookies_from_browser), "extract_flat": True,
            "playlistend": limit, "noplaylist": False}
    opts.pop("playlist_items", None)
    return opts


def _started(info: dict) -> float | None:
    """When a video started, from whichever date its listing or page gives (UTC seconds)."""
    for key in ("timestamp", "release_timestamp"):
        if info.get(key):
            return float(info[key])
    day = str(info.get("upload_date") or "")
    if re.fullmatch(r"\d{8}", day):
        return datetime.strptime(day, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()
    return None


def latest_uploads(url: str, limit: int = 10, cookies_from_browser: str | None = None) -> tuple[str, list[Upload]]:
    """(channel name, its newest finished videos, newest first). Live and upcoming streams are left out: they are
    picked up once they have ended."""
    name, found = "", {}
    for page in channel_lists(url):
        opts = _listing_options(limit, cookies_from_browser)
        try:
            with _site_errors(page), _session(opts) as ydl:
                info = ydl.extract_info(page, download=False)
        except ClippyError:
            if len(channel_lists(url)) > 1:
                continue
            raise
        name = name or str(info.get("channel") or info.get("uploader") or info.get("title") or "")
        for entry in list(info.get("entries") or [])[:limit]:
            if not entry or entry.get("_type") == "playlist" or entry.get("live_status") in NOT_FINISHED:
                continue
            link = (entry.get("url") or entry.get("webpage_url") or "").split("#__youtubedl_smuggle", 1)[0]
            if entry.get("id") and link.startswith("http"):
                found.setdefault(str(entry["id"]), Upload(str(entry["id"]), link, str(entry.get("title") or "")))
    if not found and not name:
        raise ClippyError(key="error.watch_empty")
    return re.sub(r"\s+-\s+(Videos|Live|Streams|All Videos.*)$", "", name).strip(), list(found.values())


def chat_file(downloads: Path, vid: str) -> Path:
    return downloads / f"{vid}.live_chat.json"


def fetch_chat(remote: Remote, downloads: Path, cookies_from_browser: str | None = None) -> Path | None:
    """The chat of a stream replay, saved next to the video (the cache and a chat's deletion treat it alike)."""
    target = chat_file(downloads, remote.vid)
    if target.is_file():
        return target
    if not remote.has_chat:
        return None
    log("[download] stream chat replay")
    partial = downloads / f".part-{remote.vid}-chat-{uuid.uuid4().hex[:8]}"
    partial.mkdir()
    try:
        opts = {**_ydl_options(partial, 2160, cookies_from_browser, "chat"), "skip_download": True,
                "writesubtitles": True, "subtitleslangs": ["live_chat"]}
        with _site_errors(remote.url), _session(opts) as ydl:
            ydl.extract_info(remote.url, download=True)
        done = next(partial.glob("chat*.json"), None)
        return move_atomic(done, target) if done is not None else None
    finally:
        remove_partial(partial)


def meta_file(downloads: Path, vid: str) -> Path:
    return downloads / f"{vid}.meta.json"


def _spans(items, *keys: str) -> list[dict]:
    return [{k: item[k] for k in ("start_time", "end_time", *keys)} for item in items or []
            if isinstance(item, dict) and all(isinstance(item.get(k), (int, float)) for k in ("start_time", "end_time"))
            and all(item.get(k) is not None for k in keys)]


def keep_meta(remote: Remote, downloads: Path) -> dict:
    """The page's Most Replayed curve and chapters, saved next to the video (the cache and a chat's deletion treat it
    alike). The page is read on every run, also for a video already here, so they stay fresh; a reading without them
    keeps what was saved."""
    meta = {"heatmap": _spans(remote.info.get("heatmap"), "value"),
            "chapters": [{**c, "title": str(c["title"])[:200]} for c in _spans(remote.info.get("chapters"), "title")]}
    if meta["heatmap"] or meta["chapters"]:
        write_json(meta_file(downloads, remote.vid), meta)
    return read_meta(downloads, remote.vid)


def read_meta(downloads: Path, vid: str) -> dict:
    """{"heatmap": [...], "chapters": [...]} saved for a video, empty lists when there is none."""
    try:
        data = read_json(meta_file(downloads, vid))
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    return {"heatmap": _spans(data.get("heatmap"), "value"), "chapters": _spans(data.get("chapters"), "title")}


def fetch(source: str, downloads: Path, max_height: int = 2160, cookies_from_browser: str | None = None) -> tuple[Path, str]:
    """(video file, title). Best SDR quality up to 4K: a 9:16 crop of 4K is downscaled and stays sharp."""
    if not is_url(source):
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise ClippyError(f"No such file: {path}")
        return path, path.stem
    remote = probe(source, downloads, max_height, cookies_from_browser)
    if (cached := _downloaded(downloads, remote.vid)) is not None:
        log(f"[download] already here: {cached.name}")
        return cached, remote.title
    return fetch_video(remote, downloads, max_height, cookies_from_browser), remote.title


def past_streams(page: str, limit: int = 5) -> list[Upload]:
    """The newest finished entries of one listing page (a YouTube streams tab, a Twitch channel's past broadcasts, a
    Kick channel's videos), newest first, each with its start time when the listing gives it."""
    with _site_errors(page), _session(_listing_options(limit)) as ydl:
        info = ydl.extract_info(page, download=False)
    found = []
    for entry in list(info.get("entries") or [])[:limit]:
        if (not entry or entry.get("_type") == "playlist" or entry.get("live_status") in NOT_FINISHED
                or entry.get("is_live")):
            continue
        link = (entry.get("url") or entry.get("webpage_url") or "").split("#__youtubedl_smuggle", 1)[0]
        if entry.get("id") and link.startswith("http"):
            found.append(Upload(str(entry["id"]), link, str(entry.get("title") or ""), _started(entry)))
    return found


def stream_moment(url: str) -> tuple[float | None, bool]:
    """(start time, finished) of one stream video: the page is read without choosing a format."""
    opts = {**_listing_options(1), "extract_flat": False, "noplaylist": True, "ignore_no_formats_error": True}
    with _site_errors(url), _session(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return _started(info), info.get("live_status") not in NOT_FINISHED and not info.get("is_live")
