"""TikTok through the Content Posting API: desktop sign-in with PKCE, then a chunked file upload.

"draft" sends the video to the TikTok app's inbox, where the user finishes and posts it; "private" posts it
straight away but only visible to the user, which is all TikTok allows before its audit."""

from __future__ import annotations

import os
import time
from dataclasses import replace
from urllib.parse import urlencode

from ..tools import ClippyError, report
from . import (TIMEOUT, UPLOAD_TIMEOUT, PostResult, PostText, Reader, Tokens, Upload, bearer, check_length, checked,
               env_client)
from .oauth import hex_challenge

AUTH = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com/v2"
SCOPES = "user.info.basic,video.upload,video.publish"
MODES = ("draft", "private")
SINGLE_LIMIT = 64 * 1024 * 1024
CHUNK = 10 * 1024 * 1024
LONGEST = 600
CAPTION = 2200
POLL_SECONDS, POLL_TRIES = 5, 120
DONE = {"draft": {"SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"}, "private": {"PUBLISH_COMPLETE"}}


def chunks(size: int) -> tuple[int, int]:
    """TikTok's chunk rule: one chunk up to 64 MB, else 10 MB chunks with the remainder in the last one."""
    if size <= SINGLE_LIMIT:
        return size, 1
    return CHUNK, size // CHUNK


class TikTok:
    name, label, oauth = "tiktok", "TikTok", True

    def __init__(self, sleep=time.sleep):
        self.sleep = sleep

    def client(self) -> tuple[str, ...]:
        return env_client(self.label, "CLIPPY_TIKTOK_CLIENT_KEY", "CLIPPY_TIKTOK_CLIENT_SECRET")

    def configured(self) -> bool:
        try:
            self.client()
        except ClippyError:
            return False
        return True

    def auth_url(self, redirect: str, state: str, verifier: str) -> str:
        key, _ = self.client()
        return AUTH + "?" + urlencode({
            "client_key": key, "scope": os.environ.get("CLIPPY_TIKTOK_SCOPES") or SCOPES, "response_type": "code",
            "redirect_uri": redirect, "state": state, "code_challenge": hex_challenge(verifier),
            "code_challenge_method": "S256"})

    def _token(self, http, form: dict) -> dict:
        key, secret = self.client()
        return checked(http.post(f"{API}/oauth/token/", data={"client_key": key, "client_secret": secret, **form},
                                 headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=TIMEOUT),
                       self.label)

    def exchange(self, http, code: str, verifier: str, redirect: str, now: float) -> Tokens:
        data = self._token(http, {"code": code, "grant_type": "authorization_code", "redirect_uri": redirect,
                                  "code_verifier": verifier})
        tokens = Tokens(access_token=data["access_token"], refresh_token=data.get("refresh_token", ""),
                        expires_at=now + float(data.get("expires_in") or 86400), user_id=str(data.get("open_id") or ""))
        user = checked(http.get(f"{API}/user/info/", params={"fields": "open_id,display_name"},
                                headers=bearer(tokens.access_token), timeout=TIMEOUT), self.label)
        tokens.name = str(((user.get("data") or {}).get("user") or {}).get("display_name") or "")
        return tokens

    def needs_refresh(self, tokens: Tokens, now: float) -> bool:
        return tokens.expires_at - now < 300

    def refresh(self, http, tokens: Tokens, now: float) -> Tokens:
        if not tokens.refresh_token:
            raise ClippyError(key="error.account_expired", platform=self.label)
        data = self._token(http, {"grant_type": "refresh_token", "refresh_token": tokens.refresh_token})
        return replace(tokens, access_token=data["access_token"], expires_at=now + float(data.get("expires_in") or 86400),
                       refresh_token=data.get("refresh_token") or tokens.refresh_token)

    def _call(self, http, tokens: Tokens, path: str, body: dict) -> dict:
        return checked(http.post(f"{API}{path}", json=body, timeout=TIMEOUT, headers={
            **bearer(tokens.access_token), "Content-Type": "application/json; charset=UTF-8"}), self.label).get("data") or {}

    def publish(self, http, tokens: Tokens, upload: Upload, post: PostText, options: dict) -> PostResult:
        check_length(self.label, upload, LONGEST)
        mode = options.get("mode") if options.get("mode") in MODES else "draft"
        size = upload.size
        chunk, count = chunks(size)
        source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}
        if mode == "draft":
            init = self._call(http, tokens, "/post/publish/inbox/video/init/", {"source_info": source})
        else:
            creator = self._call(http, tokens, "/post/publish/creator_info/query/", {})
            if "SELF_ONLY" not in (creator.get("privacy_level_options") or []):
                raise ClippyError(key="error.platform_rejected", platform=self.label, detail="SELF_ONLY not offered")
            init = self._call(http, tokens, "/post/publish/video/init/", {"source_info": source, "post_info": {
                "title": post.caption[:CAPTION], "privacy_level": "SELF_ONLY", "disable_comment": False,
                "disable_duet": False, "disable_stitch": False}})
        publish_id, target = init.get("publish_id"), init.get("upload_url")
        if not publish_id or not target:
            raise ClippyError(key="error.platform_rejected", platform=self.label, detail="no upload address")
        for index in range(count):
            start = index * chunk
            length = size - start if index == count - 1 else chunk
            with Reader(upload.path, start, length, size) as body:
                answer = http.put(target, data=body, timeout=UPLOAD_TIMEOUT, headers={
                    "Content-Type": "video/mp4", "Content-Range": f"bytes {start}-{start + length - 1}/{size}"})
            if answer.status_code not in (200, 201, 206):
                checked(answer, self.label)
                raise ClippyError(key="error.platform_rejected", platform=self.label, detail=str(answer.status_code))
        self._wait(http, tokens, publish_id, DONE[mode])
        report(1.0)
        return PostResult(url="", remote_id=str(publish_id), state=mode)

    def _wait(self, http, tokens: Tokens, publish_id: str, done: set[str]) -> None:
        for _ in range(POLL_TRIES):
            state = self._call(http, tokens, "/post/publish/status/fetch/", {"publish_id": publish_id})
            status = state.get("status")
            if status in done:
                return
            if status == "FAILED":
                raise ClippyError(key="error.platform_rejected", platform=self.label,
                                  detail=str(state.get("fail_reason") or status)[:200])
            self.sleep(POLL_SECONDS)
        raise ClippyError(key="error.platform_slow", platform=self.label)
