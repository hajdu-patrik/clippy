"""Streamers by name: a directory of the biggest ones with their channels, finding a name in a message, and the newest
finished stream across YouTube, Twitch and Kick."""

from __future__ import annotations

import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, NamedTuple

import requests

from .download import Upload, past_streams, stream_moment
from .tools import ClippyError, log

PLATFORMS = ("youtube", "twitch", "kick")
LABELS = {"youtube": "YouTube", "twitch": "Twitch", "kick": "Kick"}
HOMES = {"youtube": "https://www.youtube.com/@{h}", "twitch": "https://www.twitch.tv/{h}",
         "kick": "https://kick.com/{h}"}
PAGES = {"youtube": "https://www.youtube.com/@{h}/streams", "twitch": "https://www.twitch.tv/{h}/videos?filter=archives",
         "kick": "https://kick.com/{h}/videos"}
HANDLE = re.compile(r"[A-Za-z0-9_.-]{3,30}")
KICK_CHANNEL = "https://kick.com/api/v2/channels/{slug}"
KICK_VIDEOS = "https://web.kick.com/api/v1/channels/{creator}/videos"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0.0.0 Safari/537.36")
LIMIT = 6
PROBES = 3
SUFFIXES = frozenset((
    "", "t", "at", "et", "ot", "nak", "nek", "tol", "rol", "bol", "ra", "re", "ba", "be", "ig", "val", "vel", "tal",
    "tel", "del", "dal", "ert", "hoz", "hez", "nal", "nel", "on", "en", "ben", "ban", "kent", "ek", "ak"))
RESERVED = frozenset(("videos", "video", "directory", "categories", "search", "downloads", "settings", "about"))


class Channel(NamedTuple):
    platform: str
    handle: str

    @property
    def home(self) -> str:
        return HOMES[self.platform].format(h=self.handle)

    @property
    def page(self) -> str:
        return PAGES[self.platform].format(h=self.handle)


@dataclass(frozen=True)
class Streamer:
    name: str
    aliases: tuple[str, ...] = ()
    twitch: str = ""
    kick: str = ""
    youtube: str = ""

    def handles(self) -> dict[str, str]:
        return {p: getattr(self, p) for p in PLATFORMS if getattr(self, p)}

    def channels(self) -> list[Channel]:
        return [Channel(p, h) for p, h in self.handles().items() if HANDLE.fullmatch(h)]

    def urls(self) -> dict[str, str]:
        return {c.platform: c.home for c in self.channels()}


@dataclass(frozen=True)
class Stream:
    """A finished stream picked for clipping."""

    streamer: str
    platform: str
    url: str
    title: str
    when: float

    @property
    def label(self) -> str:
        return LABELS[self.platform]

    @property
    def date(self) -> str:
        return datetime.fromtimestamp(self.when, timezone.utc).strftime("%Y-%m-%d")


def fold(text: str) -> str:
    """Lowercase without accents, so "Legújabb" and "legujabb" are the same word."""
    plain = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in plain if not unicodedata.combining(c)).lower()


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", fold(text))


def _streamer(name: str, aliases: str = "", twitch: str = "", kick: str = "", youtube: str = "") -> Streamer:
    spelled = words(name)
    forms = {" ".join(spelled), "".join(spelled), *(" ".join(words(a)) for a in aliases.split(",") if a.strip())}
    return Streamer(name, tuple(sorted(forms, key=lambda f: (-len(f.split()), f))), twitch, kick, youtube)


DIRECTORY = (
    _streamer("Kai Cenat", twitch="kaicenat"),
    _streamer("IShowSpeed", "i show speed", twitch="ishowspeed", youtube="IShowSpeed"),
    _streamer("xQc", "x q c", twitch="xqc", kick="xqc"),
    _streamer("Adin Ross", "adin", twitch="adinross", kick="adinross", youtube="adinross"),
    _streamer("N3on", kick="n3on", youtube="N3ON"),
    _streamer("Ibai", "ibai llanos", twitch="ibai", youtube="IbaiLlanos"),
    _streamer("Jynxzi", twitch="jynxzi", youtube="Jynxzi"),
    _streamer("CaseOh", twitch="caseoh_"),
    _streamer("TheGrefg", "grefg", twitch="thegrefg", youtube="TheGrefg"),
    _streamer("AuronPlay", "auron", twitch="auronplay", youtube="auronplay"),
    _streamer("Asmongold", "asmon, zackrawrr", twitch="zackrawrr"),
    _streamer("shroud", twitch="shroud", youtube="shroud"),
    _streamer("summit1g", twitch="summit1g", youtube="summit1g"),
    _streamer("Pokimane", "poki", twitch="pokimane", youtube="pokimane"),
    _streamer("HasanAbi", "hasan piker, hasan", twitch="hasanabi", youtube="HasanAbi"),
    _streamer("Amouranth", twitch="amouranth", kick="amouranth"),
    _streamer("Trainwreckstv", "trainwrecks, trainwreck", kick="trainwreckstv"),
    _streamer("Sketch", twitch="sketch"),
    _streamer("Stable Ronaldo", twitch="stableronaldo"),
    _streamer("Fanum", "justfanum", twitch="fanum", youtube="fanum"),
    _streamer("Clix", twitch="clix"),
    _streamer("Tfue", twitch="tfue"),
    _streamer("Ninja", twitch="ninja", youtube="Ninja"),
    _streamer("TheBurntPeanut", "burnt peanut", twitch="theburntpeanut", kick="theburntpeanut",
              youtube="TheBurntPeanut"),
    _streamer("Caedrel", twitch="caedrel", youtube="Caedrel"),
    _streamer("Rubius", "el rubius", twitch="rubius"),
    _streamer("westcol", kick="westcol"),
    _streamer("Gaules", twitch="gaules", youtube="Gaules"),
    _streamer("Rybson", "rybsonlol", kick="rybsonlol"),
    _streamer("StRoGo", twitch="strogo"),
    _streamer("eliasn97", twitch="eliasn97", youtube="eliasn97"),
    _streamer("aminematue", twitch="aminematue"),
    _streamer("absi", kick="absi"),
    _streamer("Maherco", kick="maherco"),
    _streamer("3mr", kick="3mr"),
    _streamer("YourRAGE", "yourragegaming", twitch="yourragegaming"),
    _streamer("NICKMERCS", twitch="nickmercs", youtube="NICKMERCS"),
    _streamer("TimTheTatman", "tatman", twitch="timthetatman", youtube="TimTheTatman"),
    _streamer("Ludwig", twitch="ludwig", youtube="ludwig"),
    _streamer("Valkyrae", twitch="valkyrae", youtube="Valkyrae"),
    _streamer("Mizkif", youtube="Mizkif"),
    _streamer("Esfand", "esfandtv", twitch="esfandtv", youtube="EsfandTV"),
    _streamer("Plaqueboymax", twitch="plaqueboymax"),
    _streamer("Agent00", twitch="agent00", youtube="Agent00"),
    _streamer("2xRaKai", "rakai", twitch="2xrakai"),
    _streamer("Typical Gamer", youtube="TypicalGamer"),
    _streamer("latwogang", youtube="latwogang"),
    _streamer("Spreen", kick="spreen"),
    _streamer("Knekro", twitch="knekro"),
    _streamer("Elded", kick="elded"),
    _streamer("Lirik", twitch="lirik", youtube="LIRIK"),
    _streamer("Sykkuno", twitch="sykkuno", youtube="Sykkuno"),
)
FORMS = sorted(((tuple(form.split()), s) for s in DIRECTORY for form in s.aliases), key=lambda f: -len(f[0]))

SITE_HANDLES = (
    (re.compile(r"(?<![\w./@-])(?:www\.)?twitch\.tv/([A-Za-z0-9_]{3,25})(?!\w)", re.I), ("twitch",)),
    (re.compile(r"(?<![\w./@-])(?:www\.)?kick\.com/([A-Za-z0-9_-]{3,25})(?!\w)", re.I), ("kick",)),
    (re.compile(r"(?<![\w./@-])(?:www\.)?youtube\.com/@([A-Za-z0-9_.-]{3,30})(?!\w)", re.I), ("youtube",)),
    (re.compile(r"(?<![\w./@-])@([A-Za-z0-9_]{3,25})(?![\w@])"), PLATFORMS),
)


def listing() -> list[dict]:
    """The directory for the browser: each streamer's name and channel links."""
    return [{"name": s.name, "urls": s.urls()} for s in DIRECTORY]


def _matches(tokens: list[str], at: int, form: tuple[str, ...]) -> bool:
    """The form's words from position `at`; the last one may carry a Hungarian ending ("N3on-nak", "IShowSpeednek")."""
    end = at + len(form)
    if end > len(tokens) or tokens[at:end - 1] != list(form[:-1]):
        return False
    last = tokens[end - 1]
    return last.startswith(form[-1]) and last[len(form[-1]):] in SUFFIXES


def resolve(text: str) -> Streamer | None:
    """The first streamer of the directory a message names, or one it points to with a handle (twitch.tv/name,
    kick.com/name, @name); a name that is neither is not guessed."""
    tokens = words(text)
    for at in range(len(tokens)):
        found = next((s for form, s in FORMS if _matches(tokens, at, form)), None)
        if found:
            return found
    for pattern, platforms in SITE_HANDLES:
        match = pattern.search(text or "")
        if match and match.group(1).lower() not in RESERVED:
            handle = match.group(1)
            return Streamer(handle, (), **{p: handle for p in platforms})
    return None


def _kick_json(url: str):
    try:
        answer = requests.get(url, headers={"User-Agent": BROWSER_UA, "Accept": "application/json"}, timeout=15)
        return answer.json() if answer.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def kick_api_streams(slug: str, limit: int = LIMIT) -> list[Upload]:
    """A Kick channel's past broadcasts from the JSON that yt-dlp reads too, for when yt-dlp cannot list them;
    nothing when the site does not answer."""
    if not HANDLE.fullmatch(slug):
        return []
    channel = _kick_json(KICK_CHANNEL.format(slug=slug))
    creator = channel.get("id") if isinstance(channel, dict) else None
    if not isinstance(creator, int):
        return []
    return kick_uploads(_kick_json(KICK_VIDEOS.format(creator=creator)), slug)[:limit]


def _utc(text) -> float | None:
    try:
        return datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def kick_uploads(data, slug: str) -> list[Upload]:
    """Kick's video list as finished, public uploads, newest first; a broadcast that is still on is left out."""
    rows = data.get("data") if isinstance(data, dict) else None
    found = []
    for row in rows if isinstance(rows, list) else []:
        vid = str(row.get("id") or "") if isinstance(row, dict) else ""
        if not re.fullmatch(r"[\w-]{8,64}", vid) or row.get("is_live") or row.get("status") not in (None, "public"):
            continue
        found.append(Upload(vid, f"https://kick.com/{slug}/videos/{vid}", str(row.get("title") or ""),
                            _utc(row.get("start_time"))))
    return sorted(found, key=lambda u: -(u.when or 0))


def _first_finished(streamer: Streamer, channel: Channel, lister: Callable, moment: Callable,
                    kick_api: Callable) -> Stream | None:
    try:
        found = lister(channel.page, LIMIT)
    except ClippyError:
        found = []
    if not found and channel.platform == "kick":
        found = kick_api(channel.handle, LIMIT)
    for upload in found[:PROBES]:
        when = upload.when
        if when is None or channel.platform == "twitch":
            try:
                probed, finished = moment(upload.url)
            except ClippyError:
                continue
            if not finished:
                continue
            when = when or probed
        if when is not None:
            return Stream(streamer.name, channel.platform, upload.url, upload.title, when)
    return None


def _newest(streamer: Streamer, channel: Channel, lister: Callable, moment: Callable,
            kick_api: Callable) -> Stream | None:
    try:
        return _first_finished(streamer, channel, lister, moment, kick_api)
    except Exception as exc:
        log(f"[streamers] {channel.page}: {type(exc).__name__}: {exc}")
        return None


def latest_stream(streamer: Streamer, lister: Callable = past_streams, moment: Callable = stream_moment,
                  kick_api: Callable = kick_api_streams) -> Stream:
    """The newest finished stream across the streamer's channels; one that is on right now is skipped for the one
    before it. Raises error.stream_none when no platform has one."""
    channels = streamer.channels()
    with ThreadPoolExecutor(max_workers=max(1, len(channels))) as pool:
        picked = list(pool.map(lambda c: _newest(streamer, c, lister, moment, kick_api), channels))
    found = [s for s in picked if s]
    if not found:
        raise ClippyError(key="error.stream_none", streamer=streamer.name)
    return max(found, key=lambda s: s.when)
