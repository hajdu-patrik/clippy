"""The account types, in the order Settings shows them. A new platform is one class in its own module plus a line here."""

from __future__ import annotations

from .base import Platform
from .facebook import Facebook
from .instagram import Instagram
from .kick import Kick
from .tiktok import TikTok
from .twitch import Twitch
from .x import X
from .youtube import YouTube

ADAPTERS: tuple[type[Platform], ...] = (YouTube, Instagram, Facebook, TikTok, X, Twitch, Kick)
PLATFORMS = tuple(a.name for a in ADAPTERS)
POSTING = tuple(a.name for a in ADAPTERS if a.posting)
SELF_SCHEDULING = tuple(a.name for a in ADAPTERS if a.schedules)
LABELS = {a.name: a.label for a in ADAPTERS}


def default_platforms() -> dict[str, Platform]:
    return {a.name: a() for a in ADAPTERS}
