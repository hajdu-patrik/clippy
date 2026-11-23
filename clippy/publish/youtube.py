"""YouTube Shorts through the Data API: Google sign-in for a desktop app, then a resumable upload in chunks."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from ..text import no_emoji
from ..tools import ClippyError, log, report
from . import TIMEOUT, UPLOAD_TIMEOUT, PostResult, PostText, Reader, Tokens, Upload, bearer, check_length, checked
from .base import OAuthPlatform

AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMBNAILS = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
THUMBNAIL_MAX = 2 * 1024 * 1024
SCOPES = ("https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly "
          "https://www.googleapis.com/auth/yt-analytics.readonly")
CHUNK = 8 * 1024 * 1024
LONGEST = 180
PRIVACY = ("public", "unlisted", "private")
TRIES = 3
AHEAD = 120.0


def no_brackets(text: str) -> str:
    return re.sub(r"[<>]", "", text or "").strip()


def tags_of(hashtags) -> list[str]:
    out, used = [], 0
    for tag in hashtags:
        word = str(tag).lstrip("#").strip()
        if word and used + len(word) + 1 <= 400:
            out.append(word)
            used += len(word) + 1
    return out


def usable_cover(image: Path) -> bool:
    return image.is_file() and image.stat().st_size <= THUMBNAIL_MAX


class YouTube(OAuthPlatform):
    name, label = "youtube", "YouTube"
    env = ("CLIPPY_YOUTUBE_CLIENT_ID", "CLIPPY_YOUTUBE_CLIENT_SECRET")
    console = "https://console.cloud.google.com/apis/credentials"
    auth_endpoint, token_url, scopes = AUTH, TOKEN, SCOPES
    extra_auth = {"access_type": "offline", "prompt": "consent"}
    schedules = True

    def identify(self, http, tokens: Tokens) -> None:
        channels = checked(http.get(f"{API}/channels", params={"part": "snippet", "mine": "true"},
                                    headers=bearer(tokens.access_token), timeout=TIMEOUT), self.label)
        items = channels.get("items") or []
        if items:
            tokens.name, tokens.user_id = items[0]["snippet"]["title"], items[0]["id"]

    def publish(self, http, tokens: Tokens, upload: Upload, post: PostText, options: dict) -> PostResult:
        check_length(self.label, upload, LONGEST)
        privacy = options.get("privacy") if options.get("privacy") in PRIVACY else "public"
        size = upload.size
        body = {"snippet": {"title": no_emoji(no_brackets(post.title))[:100] or "Short",
                            "description": no_emoji(no_brackets(post.caption))[:4900], "tags": tags_of(post.hashtags),
                            "categoryId": str(options.get("category") or "22")},
                "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
        at = options.get("publish_at")
        planned = isinstance(at, (int, float)) and at > time.time() + AHEAD
        if planned:
            body["status"] |= {"privacyStatus": "private",
                               "publishAt": datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        start = http.post(UPLOAD, params={"uploadType": "resumable", "part": "snippet,status"}, json=body,
                          headers={**bearer(tokens.access_token), "X-Upload-Content-Length": str(size),
                                   "X-Upload-Content-Type": "video/mp4"}, timeout=TIMEOUT)
        checked(start, self.label)
        session = start.headers.get("Location")
        if not session:
            raise ClippyError(key="error.platform_rejected", platform=self.label, detail="no upload session")
        video = self._send(http, tokens, session, upload, size)
        cover = upload.path.with_suffix(".jpg")
        pending = usable_cover(cover) and not self.set_cover(http, tokens, video["id"], cover)
        got = (video.get("status") or {}).get("privacyStatus") or privacy
        state = "scheduled" if planned else got if got == privacy else f"{got}_until_audit"
        return PostResult(url=f"https://youtube.com/shorts/{video['id']}", remote_id=video["id"], state=state,
                          cover_pending=pending)

    def set_cover(self, http, tokens: Tokens, remote_id: str, image: Path) -> bool:
        """The clip's cover (its title written on a frame) as the video's thumbnail; a refusal never fails the post."""
        if not usable_cover(image):
            return False
        try:
            checked(http.post(THUMBNAILS, params={"videoId": remote_id, "uploadType": "media"}, data=image.read_bytes(),
                              headers={**bearer(tokens.access_token), "Content-Type": "image/jpeg"},
                              timeout=UPLOAD_TIMEOUT), self.label)
        except (ClippyError, OSError, requests.RequestException) as exc:
            log(f"[publish] YouTube thumbnail skipped: {exc}")
            return False
        return True

    def _send(self, http, tokens: Tokens, session: str, upload: Upload, size: int) -> dict:
        offset, failures = 0, 0
        while True:
            length = min(CHUNK, size - offset)
            try:
                with Reader(upload.path, offset, length, size) as body:
                    answer = http.put(session, data=body, timeout=UPLOAD_TIMEOUT, headers={
                        **bearer(tokens.access_token), "Content-Type": "video/mp4",
                        "Content-Range": f"bytes {offset}-{offset + length - 1}/{size}"})
            except OSError:
                answer = None
            if answer is not None and answer.status_code in (200, 201):
                report(1.0)
                return checked(answer, self.label)
            if answer is not None and answer.status_code == 308:
                offset, failures = self._offset(answer), 0
                continue
            if answer is not None and answer.status_code < 500:
                checked(answer, self.label)
            failures += 1
            if failures >= TRIES:
                raise ClippyError(key="error.platform_rejected", platform=self.label, detail="upload failed")
            probe = http.put(session, headers={**bearer(tokens.access_token), "Content-Range": f"bytes */{size}"},
                             timeout=TIMEOUT)
            if probe.status_code in (200, 201):
                return checked(probe, self.label)
            offset = self._offset(probe)

    @staticmethod
    def _offset(answer) -> int:
        found = re.search(r"-(\d+)$", answer.headers.get("Range") or "")
        return int(found.group(1)) + 1 if found else 0
