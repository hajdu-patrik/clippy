"""YouTube Shorts through the Data API: Google sign-in for a desktop app, then a resumable upload in chunks."""

from __future__ import annotations

import re
from dataclasses import replace
from urllib.parse import urlencode

from ..tools import ClippyError, report
from . import (TIMEOUT, UPLOAD_TIMEOUT, PostResult, PostText, Reader, Tokens, Upload, bearer, check_length, checked,
               env_client)
from .oauth import s256_challenge

AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPES = ("https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly "
          "https://www.googleapis.com/auth/yt-analytics.readonly")
CHUNK = 8 * 1024 * 1024
LONGEST = 180
PRIVACY = ("public", "unlisted", "private")
TRIES = 3


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


class YouTube:
    name, label, oauth = "youtube", "YouTube", True

    def client(self) -> tuple[str, ...]:
        return env_client(self.label, "CLIPPY_YOUTUBE_CLIENT_ID", "CLIPPY_YOUTUBE_CLIENT_SECRET")

    def configured(self) -> bool:
        try:
            self.client()
        except ClippyError:
            return False
        return True

    def auth_url(self, redirect: str, state: str, verifier: str) -> str:
        client_id, _ = self.client()
        return AUTH + "?" + urlencode({
            "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPES,
            "access_type": "offline", "prompt": "consent", "state": state,
            "code_challenge": s256_challenge(verifier), "code_challenge_method": "S256"})

    def exchange(self, http, code: str, verifier: str, redirect: str, now: float) -> Tokens:
        client_id, secret = self.client()
        data = checked(http.post(TOKEN, data={
            "code": code, "client_id": client_id, "client_secret": secret, "redirect_uri": redirect,
            "grant_type": "authorization_code", "code_verifier": verifier}, timeout=TIMEOUT), self.label)
        tokens = Tokens(access_token=data["access_token"], refresh_token=data.get("refresh_token", ""),
                        expires_at=now + float(data.get("expires_in") or 3600), scope=str(data.get("scope") or ""))
        channels = checked(http.get(f"{API}/channels", params={"part": "snippet", "mine": "true"},
                                    headers=bearer(tokens.access_token), timeout=TIMEOUT), self.label)
        items = channels.get("items") or []
        if items:
            tokens.name, tokens.user_id = items[0]["snippet"]["title"], items[0]["id"]
        return tokens

    def needs_refresh(self, tokens: Tokens, now: float) -> bool:
        return tokens.expires_at - now < 300

    def refresh(self, http, tokens: Tokens, now: float) -> Tokens:
        if not tokens.refresh_token:
            raise ClippyError(key="error.account_expired", platform=self.label)
        client_id, secret = self.client()
        data = checked(http.post(TOKEN, data={
            "client_id": client_id, "client_secret": secret, "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token}, timeout=TIMEOUT), self.label)
        return replace(tokens, access_token=data["access_token"], expires_at=now + float(data.get("expires_in") or 3600),
                       refresh_token=data.get("refresh_token") or tokens.refresh_token)

    def publish(self, http, tokens: Tokens, upload: Upload, post: PostText, options: dict) -> PostResult:
        check_length(self.label, upload, LONGEST)
        privacy = options.get("privacy") if options.get("privacy") in PRIVACY else "public"
        size = upload.size
        body = {"snippet": {"title": no_brackets(post.title)[:100] or "Short",
                            "description": no_brackets(post.caption)[:4900], "tags": tags_of(post.hashtags),
                            "categoryId": "22"},
                "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
        start = http.post(UPLOAD, params={"uploadType": "resumable", "part": "snippet,status"}, json=body,
                          headers={**bearer(tokens.access_token), "X-Upload-Content-Length": str(size),
                                   "X-Upload-Content-Type": "video/mp4"}, timeout=TIMEOUT)
        checked(start, self.label)
        session = start.headers.get("Location")
        if not session:
            raise ClippyError(key="error.platform_rejected", platform=self.label, detail="no upload session")
        video = self._send(http, tokens, session, upload, size)
        got = (video.get("status") or {}).get("privacyStatus") or privacy
        return PostResult(url=f"https://youtube.com/shorts/{video['id']}", remote_id=video["id"],
                          state=got if got == privacy else f"{got}_until_audit")

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
