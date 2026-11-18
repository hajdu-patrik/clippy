"""Facebook Page Reels through the Graph API: Facebook Login for the Page, then a three-step Reels upload.

The Page's access token (it does not expire) is what Clippy keeps; the user token is used once to find the Page."""

from __future__ import annotations

import os
import time

from ..tools import ClippyError, report
from . import TIMEOUT, UPLOAD_TIMEOUT, PostResult, PostText, Reader, Tokens, Upload, check_length, checked
from .base import OAuthPlatform

VERSION = "v27.0"
GRAPH = f"https://graph.facebook.com/{VERSION}"
AUTH = f"https://www.facebook.com/{VERSION}/dialog/oauth"
SCOPES = "pages_show_list,pages_read_engagement,pages_manage_posts"
RUPLOAD = f"https://rupload.facebook.com/video-upload/{VERSION}"
LONGEST = 90
POLL_SECONDS, POLL_TRIES = 5, 120
FAILED = {"error", "expired", "upload_failed"}


class Facebook(OAuthPlatform):
    name, label, renews = "facebook", "Facebook", True
    env = ("CLIPPY_FACEBOOK_APP_ID", "CLIPPY_FACEBOOK_APP_SECRET")
    auth_endpoint, token_url, scopes, challenge = AUTH, f"{GRAPH}/oauth/access_token", SCOPES, None

    def __init__(self, sleep=time.sleep):
        self.sleep = sleep

    def auth_params(self, params: dict) -> dict:
        """An app with Facebook Login for Business names a saved configuration instead of the permissions."""
        config = (os.environ.get("CLIPPY_FACEBOOK_CONFIG_ID") or "").strip()
        if config:
            params = {k: v for k, v in params.items() if k != "scope"} | {"config_id": config}
        return params

    def token_call(self, http, form: dict) -> dict:
        app_id, secret = self.client()
        data = checked(http.get(self.token_url, params={"client_id": app_id, "client_secret": secret, **form},
                                timeout=TIMEOUT), self.label)
        if not data.get("access_token"):
            raise ClippyError(key="error.platform_rejected", platform=self.label, detail="no access token")
        return data

    def exchange(self, http, code: str, verifier: str, redirect: str, now: float) -> Tokens:
        short = self.token_call(http, {"code": code, "redirect_uri": redirect})
        long = self.token_call(http, {"grant_type": "fb_exchange_token", "fb_exchange_token": short["access_token"]})
        pages = checked(http.get(f"{GRAPH}/me/accounts", timeout=TIMEOUT, params={
            "fields": "id,name,access_token", "limit": 100, "access_token": long["access_token"]}), self.label)
        wanted = (os.environ.get("CLIPPY_FACEBOOK_PAGE_ID") or "").strip()
        page = next((p for p in pages.get("data") or [] if p.get("access_token") and (not wanted or p.get("id") == wanted)),
                    None)
        if page is None:
            raise ClippyError(key="error.no_page", platform=self.label)
        return Tokens(access_token=page["access_token"], name=str(page.get("name") or ""), user_id=str(page["id"]),
                      scope=self.scope())

    def publish(self, http, tokens: Tokens, upload: Upload, post: PostText, options: dict) -> PostResult:
        check_length(self.label, upload, LONGEST)
        auth = {"access_token": tokens.access_token}
        page, size = tokens.user_id, upload.size
        video = checked(http.post(f"{GRAPH}/{page}/video_reels", params=auth, data={"upload_phase": "start"},
                                  timeout=TIMEOUT), self.label)
        video_id = video.get("video_id")
        if not video_id:
            raise ClippyError(key="error.platform_rejected", platform=self.label, detail="no upload session")
        with Reader(upload.path) as body:
            checked(http.post(video.get("upload_url") or f"{RUPLOAD}/{video_id}", data=body, timeout=UPLOAD_TIMEOUT,
                              headers={"Authorization": f"OAuth {tokens.access_token}", "offset": "0",
                                       "file_size": str(size)}), self.label)
        checked(http.post(f"{GRAPH}/{page}/video_reels", timeout=TIMEOUT, params=auth, data={
            "upload_phase": "finish", "video_id": video_id, "video_state": "PUBLISHED",
            "description": post.caption}), self.label)
        self._wait(http, str(video_id), auth)
        report(1.0)
        return PostResult(url=f"https://www.facebook.com/reel/{video_id}", remote_id=str(video_id), state="public")

    def _wait(self, http, video_id: str, auth: dict) -> None:
        for _ in range(POLL_TRIES):
            status = checked(http.get(f"{GRAPH}/{video_id}", params={**auth, "fields": "status"}, timeout=TIMEOUT),
                             self.label).get("status") or {}
            phase = status.get("publishing_phase") or {}
            if status.get("video_status") == "ready" or phase.get("status") in ("complete", "completed"):
                return
            if status.get("video_status") in FAILED or phase.get("status") == "error":
                reason = (phase.get("error") or (status.get("processing_phase") or {}).get("error") or {}).get("message")
                raise ClippyError(key="error.platform_rejected", platform=self.label,
                                  detail=str(reason or status.get("video_status") or "error")[:200])
            self.sleep(POLL_SECONDS)
        raise ClippyError(key="error.platform_slow", platform=self.label)
