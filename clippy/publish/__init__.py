"""Connecting the user's own accounts (YouTube, Instagram, Facebook, TikTok, X, Twitch, Kick) and posting finished clips
to the ones whose platform allows it (docs/publishing.md).

The user's developer apps give the client ids (in .env); the user signs in once, the tokens stay in
data/web/accounts.json and never reach the browser; every post is the user's own action."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..tools import ClippyError, report

TIMEOUT = 60
UPLOAD_TIMEOUT = 600
EXPIRED_CODES = {"invalid_grant", "access_token_invalid", "token_expired", "invalid_token", 190}


@dataclass(frozen=True)
class PostText:
    title: str
    caption: str
    hashtags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Upload:
    path: Path
    duration: float

    @property
    def size(self) -> int:
        return self.path.stat().st_size


@dataclass(frozen=True)
class PostResult:
    url: str
    remote_id: str
    state: str


@dataclass
class Tokens:
    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0
    name: str = ""
    user_id: str = ""
    scope: str = ""


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def env_client(label: str, *names: str) -> tuple[str, ...]:
    """The developer app's credentials from .env; missing ones are a clear error about the setup guide."""
    values = tuple((os.environ.get(name) or "").strip() for name in names)
    if not all(values):
        raise ClippyError(key="error.platform_not_configured", platform=label)
    return values


def check_length(label: str, upload: Upload, longest: float) -> None:
    if upload.duration > longest:
        raise ClippyError(key="error.clip_too_long_for", platform=label, max=int(longest))


def checked(response, label: str):
    """The JSON of a successful API answer; an error answer becomes a readable error with the platform's reason."""
    try:
        data = response.json() if response.content else {}
    except ValueError:
        data = {}
    error = data.get("error") if isinstance(data, dict) else None
    code = error.get("code") if isinstance(error, dict) else error
    if response.status_code < 400 and code in (None, "", "ok"):
        return data
    top = data if isinstance(data, dict) else {}
    reason = (error.get("message") if isinstance(error, dict) else None) or top.get("error_description") or top.get(
        "message") or top.get("detail") or code or response.status_code
    if "unaudited" in str(code):
        raise ClippyError(key="error.platform_audit", platform=label)
    if response.status_code == 401 or code in EXPIRED_CODES:
        raise ClippyError(key="error.account_expired", platform=label)
    if response.status_code == 429:
        raise ClippyError(key="error.platform_rate_limited", platform=label)
    raise ClippyError(key="error.platform_rejected", platform=label, detail=str(reason)[:200])


class Reader:
    """A file body for requests that reports how much of it was sent; requests takes its length from len()."""

    def __init__(self, path: Path, start: int = 0, length: int | None = None, total: int | None = None):
        self.file = open(path, "rb")
        self.file.seek(start)
        self.left = self.length = path.stat().st_size - start if length is None else length
        self.start, self.total = start, total or path.stat().st_size

    def __len__(self) -> int:
        return self.left

    def read(self, size: int = -1) -> bytes:
        size = self.left if size is None or size < 0 else min(size, self.left)
        chunk = self.file.read(size)
        self.left -= len(chunk)
        report((self.start + self.length - self.left) / max(1, self.total))
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.file.close()
        return False
