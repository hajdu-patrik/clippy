"""The user's own settings, kept in data/web/settings.json and never in the repository: the profile links that go
under every clip's post text (TikTok, Instagram…) and the daily times the user likes to post at."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from .storage import Storage
from .tools import ClippyError, write_json

MAX_LINKS, MAX_LINE = 6, 200
URL = re.compile(r"https?://\S+")
SITES = {"tiktok.com": "TikTok", "instagram.com": "Instagram", "youtube.com": "YouTube", "youtu.be": "YouTube",
         "facebook.com": "Facebook", "x.com": "X", "twitter.com": "X", "twitch.tv": "Twitch", "kick.com": "Kick",
         "threads.net": "Threads"}


def site_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").removeprefix("www.").removeprefix("m.")
    return next((name for domain, name in SITES.items() if host == domain or host.endswith("." + domain)), host)


def clean_links(text: str) -> list[str]:
    """One link per line, "Label: https://…" or a bare link (it gets the site's name as its label)."""
    out = []
    for raw in str(text or "").splitlines():
        line = " ".join(raw.split())[:MAX_LINE]
        if not line:
            continue
        found = URL.search(line)
        if found is None or not urlsplit(found.group()).hostname:
            raise ClippyError(key="error.bad_link", line=line[:60])
        out.append(line if line != found.group() else f"{site_of(line)}: {line}")
    if len(out) > MAX_LINKS:
        raise ClippyError(key="error.too_many_links", max=MAX_LINKS)
    return out


class Settings:
    def __init__(self, storage: Storage):
        self.path = storage.settings_file

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        data = data if isinstance(data, dict) else {}
        links, slots = data.get("profile_links"), data.get("post_slots")
        return {"profile_links": [str(x) for x in links][:MAX_LINKS] if isinstance(links, list) else [],
                "post_slots": [str(x) for x in slots] if isinstance(slots, list) else []}

    def profile_links(self) -> list[str]:
        return self.load()["profile_links"]

    def set_profile_links(self, text: str) -> dict:
        data = {**self.load(), "profile_links": clean_links(text)}
        write_json(self.path, data)
        return data

    def set_post_slots(self, slots: list[str]) -> dict:
        """The daily posting times, already checked (HH:MM, sorted)."""
        data = {**self.load(), "post_slots": list(slots)}
        write_json(self.path, data)
        return data
