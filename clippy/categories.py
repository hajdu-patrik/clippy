"""Games and their categories on Twitch and Kick: the most watched clips of the last day, which are the streamers'
biggest live reactions, as the source of the daily picks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

import requests

from .streamers import BROWSER_UA, KICK_CHANNEL, KICK_VIDEOS, fold
from .tools import log

TWITCH_GQL = "https://gql.twitch.tv/gql"
TWITCH_CLIENT = "kimne78kx3ncx6brgo4mv6wki5h1ko"
KICK_CLIPS = "https://kick.com/api/v2/categories/{slug}/clips?sort=view&time=day"
SLUG = re.compile(r"[a-z0-9-]{2,60}")
LANGUAGES = {"en": "EN", "hu": "HU"}
MIN_SECONDS, MAX_SECONDS = 8.0, 90.0
TWITCH_FIRST = 40
LATIN_SHARE = 0.85
KICK_CHANNELS_CHECKED = 12
ADULT = re.compile(r"(\+ ?18|18 ?\+|\bnsfw\b)", re.I)


@dataclass(frozen=True)
class Game:
    key: str
    name: str
    twitch: str
    kick: str
    tags: tuple[str, ...]
    pattern: str

    def matches(self, text: str) -> bool:
        return re.search(self.pattern, fold(text)) is not None


GAMES = (
    Game("gta-vi", "GTA VI", "grand-theft-auto-vi", "grand-theft-auto-vi", ("#GTA6", "#GTAVI"),
         r"\b(gta ?(vi|6)|grand theft auto ?(vi|6))\b"),
    Game("gta-v", "GTA V", "grand-theft-auto-v", "grand-theft-auto-v", ("#GTA5", "#GTAV"),
         r"\b(gta ?(v|5)|grand theft auto ?(v|5))\b"),
    Game("minecraft", "Minecraft", "minecraft", "minecraft", ("#Minecraft",), r"\bminecraft\b"),
    Game("fortnite", "Fortnite", "fortnite", "fortnite", ("#Fortnite",), r"\bfortnite\b"),
    Game("valorant", "Valorant", "valorant", "valorant", ("#Valorant",), r"\bvalorant\b"),
    Game("counter-strike", "Counter-Strike", "counter-strike", "counter-strike-2", ("#CS2",),
         r"\b(counter[ -]?strike( ?2)?|cs ?2)\b"),
    Game("league-of-legends", "League of Legends", "league-of-legends", "league-of-legends", ("#LeagueOfLegends",),
         r"\bleague of legends\b"),
    Game("warzone", "Warzone", "call-of-duty-warzone", "call-of-duty-warzone", ("#Warzone",), r"\bwarzone\b"),
)


@dataclass(frozen=True)
class Moment:
    vid: str
    url: str
    title: str
    streamer: str
    platform: str
    views: int
    duration: float
    when: float | None = None


def game(key: str) -> Game | None:
    return next((g for g in GAMES if g.key == key), None)


def find_game(text: str) -> Game | None:
    """The game named in a message ("GTA VI", "gta 6", "Minecraft"); the more specific name wins (GTA VI over GTA V)."""
    return next((g for g in GAMES if g.matches(text or "")), None)


def _iso(text) -> float | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _post_gql(query: str):
    answer = requests.post(TWITCH_GQL, json={"query": query}, headers={"Client-ID": TWITCH_CLIENT}, timeout=20)
    answer.raise_for_status()
    return answer.json()


def twitch_moments(g: Game, language: str = "en", post: Callable = _post_gql) -> list[Moment]:
    """The most viewed Twitch clips of the game from the last 24 hours, in the audience's language."""
    if not SLUG.fullmatch(g.twitch):
        return []
    langs = f", languages: [{LANGUAGES[language]}]" if language in LANGUAGES else ""
    query = (f'query {{ game(slug: "{g.twitch}") {{ clips(first: {TWITCH_FIRST}, criteria: {{period: LAST_DAY, '
             f'sort: VIEWS_DESC{langs}}}) {{ edges {{ node {{ slug url title viewCount durationSeconds createdAt '
             f'broadcaster {{ login }} }} }} }} }} }}')
    data = post(query)
    edges = ((((data or {}).get("data") or {}).get("game") or {}).get("clips") or {}).get("edges") or []
    found = []
    for edge in edges:
        node = (edge or {}).get("node") or {}
        url, login = str(node.get("url") or ""), str((node.get("broadcaster") or {}).get("login") or "")
        if not url.startswith("https://www.twitch.tv/") or not login:
            continue
        found.append(Moment(f"twitch:{node.get('slug')}", url, str(node.get("title") or ""), login, "Twitch",
                            int(node.get("viewCount") or 0), float(node.get("durationSeconds") or 0),
                            _iso(node.get("createdAt"))))
    return found


def _get_kick(url: str):
    answer = requests.get(url, headers={"User-Agent": BROWSER_UA, "Accept": "application/json"}, timeout=20)
    answer.raise_for_status()
    return answer.json()


def latin(text: str) -> bool:
    """Mostly Latin letters: Kick names no language, so an English audience gets clips whose titles it can read."""
    letters = [c for c in text if c.isalpha()]
    return not letters or sum(c.isascii() for c in letters) / len(letters) >= LATIN_SHARE


def kick_language(channel: str, get: Callable = _get_kick) -> str:
    """The language of a Kick channel's newest broadcast ("en", "pt"…), or "" when Kick does not say."""
    try:
        info = get(KICK_CHANNEL.format(slug=channel))
        creator = info.get("id") if isinstance(info, dict) else None
        videos = get(KICK_VIDEOS.format(creator=creator)) if isinstance(creator, int) else None
    except (requests.RequestException, ValueError):
        return ""
    rows = videos.get("data") if isinstance(videos, dict) else videos
    newest = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
    return str(newest.get("language") or "").lower()[:5]


def kick_moments(g: Game, language: str = "en", get: Callable = _get_kick) -> list[Moment]:
    """The most viewed public Kick clips of the game from the last day; Kick mixes other games into the list, so the
    clip's own category is checked, and a channel that streams in another language is left out."""
    if not SLUG.fullmatch(g.kick):
        return []
    data = get(KICK_CLIPS.format(slug=g.kick))
    found, languages = [], {}
    for clip in (data or {}).get("clips") or []:
        channel = str((clip.get("channel") or {}).get("slug") or "")
        cid, title = str(clip.get("id") or ""), str(clip.get("title") or "")
        if ((clip.get("category") or {}).get("slug") != g.kick or clip.get("privacy") != "public"
                or clip.get("is_mature") or not re.fullmatch(r"clip_\w{10,40}", cid) or not channel
                or (language == "en" and not latin(title))):
            continue
        if channel not in languages and len(languages) < KICK_CHANNELS_CHECKED:
            languages[channel] = kick_language(channel, get)
        if languages.get(channel, "") not in ("", language):
            continue
        found.append(Moment(f"kick:{cid}", f"https://kick.com/{channel}/clips/{cid}", title, channel, "Kick",
                            int(clip.get("view_count") or clip.get("views") or 0), float(clip.get("duration") or 0),
                            _iso(clip.get("created_at"))))
    return found


def top_moments(g: Game, language: str = "en", sources: tuple[Callable, ...] = (twitch_moments, kick_moments)
                ) -> list[Moment]:
    """Both platforms' best clips of the day, most viewed first; a platform that does not answer is skipped."""
    found = []
    for source in sources:
        try:
            found += source(g, language)
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            log(f"[daily] {g.name} on {source.__name__}: {type(exc).__name__}: {exc}")
    found = [m for m in found if MIN_SECONDS <= m.duration <= MAX_SECONDS and not ADULT.search(m.title)
             and (language != "en" or latin(m.title))]
    return sorted(found, key=lambda m: -m.views)
