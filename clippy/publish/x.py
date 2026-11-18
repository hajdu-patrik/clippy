"""X (Twitter) through API v2: sign-in with OAuth 2.0 and PKCE, a chunked video upload, then a post that carries it."""

from __future__ import annotations

import base64
import os
import time

from ..tools import ClippyError, report
from . import TIMEOUT, UPLOAD_TIMEOUT, PostResult, PostText, Tokens, Upload, bearer, check_length, checked
from .base import OAuthPlatform

AUTH = "https://x.com/i/oauth2/authorize"
API = "https://api.x.com/2"
SCOPES = "tweet.read tweet.write users.read media.write offline.access"
CHUNK = 4 * 1024 * 1024
LONGEST = 1200
TEXT = 280
POLL_TRIES = 120


def fit(text: str, limit: int = TEXT) -> str:
    """The post text within X's limit: whole paragraphs while they fit, the first one cut at a word if it alone is too long."""
    out = ""
    for paragraph in (p.strip() for p in text.split("\n\n") if p.strip()):
        joined = f"{out}\n\n{paragraph}" if out else paragraph
        if len(joined) > limit:
            if not out:
                cut = joined[:limit - 1].rsplit(" ", 1)[0] or joined[:limit - 1]
                out = cut.rstrip() + "…"
            break
        out = joined
    return out


class X(OAuthPlatform):
    name, label = "x", "X"
    env = ("CLIPPY_X_CLIENT_ID",)
    auth_endpoint, token_url, scopes, lifetime = AUTH, f"{API}/oauth2/token", SCOPES, 7200

    def __init__(self, sleep=time.sleep):
        self.sleep = sleep

    def client_auth(self) -> tuple[dict, dict]:
        """A public (native app) client sends its id; a confidential one with a secret signs in with Basic auth."""
        (client_id,), secret = self.client(), (os.environ.get("CLIPPY_X_CLIENT_SECRET") or "").strip()
        if not secret:
            return {"client_id": client_id}, {}
        token = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
        return {}, {"Authorization": f"Basic {token}"}

    def identify(self, http, tokens: Tokens) -> None:
        me = checked(http.get(f"{API}/users/me", headers=bearer(tokens.access_token), timeout=TIMEOUT), self.label)
        user = me.get("data") or {}
        tokens.name = f"@{user['username']}" if user.get("username") else str(user.get("name") or "")
        tokens.user_id = str(user.get("id") or "")

    def publish(self, http, tokens: Tokens, upload: Upload, post: PostText, options: dict) -> PostResult:
        check_length(self.label, upload, LONGEST)
        auth, size = bearer(tokens.access_token), upload.size
        media = checked(http.post(f"{API}/media/upload/initialize", headers=auth, timeout=TIMEOUT, json={
            "media_type": "video/mp4", "total_bytes": size, "media_category": "tweet_video"}), self.label)["data"]["id"]
        with open(upload.path, "rb") as source:
            for index, offset in enumerate(range(0, size, CHUNK)):
                source.seek(offset)
                chunk = source.read(CHUNK)
                checked(http.post(f"{API}/media/upload/{media}/append", headers=auth, timeout=UPLOAD_TIMEOUT,
                                  data={"segment_index": str(index)},
                                  files={"media": ("clip.mp4", chunk, "video/mp4")}), self.label)
                report((offset + len(chunk)) / max(1, size))
        done = checked(http.post(f"{API}/media/upload/{media}/finalize", headers=auth, timeout=TIMEOUT),
                       self.label).get("data") or {}
        self._wait(http, auth, media, done.get("processing_info"))
        created = checked(http.post(f"{API}/tweets", headers=auth, timeout=TIMEOUT, json={
            "text": fit(post.caption), "media": {"media_ids": [media]}}), self.label)["data"]["id"]
        report(1.0)
        return PostResult(url=f"https://x.com/i/status/{created}", remote_id=str(created), state="public")

    def _wait(self, http, auth: dict, media: str, info: dict | None) -> None:
        for _ in range(POLL_TRIES):
            state = (info or {}).get("state")
            if not info or state == "succeeded":
                return
            if state == "failed":
                raise ClippyError(key="error.platform_rejected", platform=self.label,
                                  detail=str((info.get("error") or {}).get("message") or state)[:200])
            self.sleep(float(info.get("check_after_secs") or 3))
            status = checked(http.get(f"{API}/media/upload", params={"command": "STATUS", "media_id": media},
                                      headers=auth, timeout=TIMEOUT), self.label)
            info = (status.get("data") or {}).get("processing_info")
        raise ClippyError(key="error.platform_slow", platform=self.label)
