"""Instagram Reels through the Instagram API with Instagram Login (Creator or Business account, no Facebook Page).

The user signs in with Instagram (Business Login): the short token is swapped for one that lasts 60 days and is
refreshed well before that. A token from the app dashboard's "Generate token" can be pasted instead."""

from __future__ import annotations

from dataclasses import replace

from ..tools import ClippyError, report
from . import TIMEOUT, UPLOAD_TIMEOUT, PostResult, PostText, Reader, Tokens, Upload, check_length, checked
from .base import OAuthPlatform

VERSION = "v26.0"
GRAPH = f"https://graph.instagram.com/{VERSION}"
REFRESH = "https://graph.instagram.com/refresh_access_token"
EXCHANGE = "https://graph.instagram.com/access_token"
AUTH = "https://www.instagram.com/oauth/authorize"
TOKEN = "https://api.instagram.com/oauth/access_token"
SCOPES = "instagram_business_basic,instagram_business_content_publish,instagram_business_manage_insights"
RUPLOAD = f"https://rupload.facebook.com/ig-api-upload/{VERSION}"
LASTS = 60 * 86400
REFRESH_WHEN_LEFT = 30 * 86400
LONGEST = 900
CAPTION = 2200


class Instagram(OAuthPlatform):
    name, label, renews, pasteable = "instagram", "Instagram", True, True
    env = ("CLIPPY_INSTAGRAM_APP_ID", "CLIPPY_INSTAGRAM_APP_SECRET")
    fields, console = ("instagram_app_id", "instagram_app_secret"), "https://developers.facebook.com/apps/"
    auth_endpoint, token_url, scopes, challenge = AUTH, TOKEN, SCOPES, None

    def connect(self, http, token: str, now: float, lasts: float = LASTS) -> Tokens:
        """Checks a token by asking who it belongs to."""
        token = "".join(str(token or "").split())
        if len(token) < 20:
            raise ClippyError(key="error.bad_token", platform=self.label)
        me = checked(http.get(f"{GRAPH}/me", params={"fields": "user_id,username", "access_token": token},
                              timeout=TIMEOUT), self.label)
        return Tokens(access_token=token, expires_at=now + lasts, name=str(me.get("username") or ""),
                      user_id=str(me.get("user_id") or me.get("id") or ""))

    def exchange(self, http, code: str, verifier: str, redirect: str, now: float) -> Tokens:
        """The sign-in's code becomes a short token, the short token one that lasts 60 days."""
        app_id, secret = self.client()
        answer = checked(http.post(TOKEN, timeout=TIMEOUT, data={
            "client_id": app_id, "client_secret": secret, "grant_type": "authorization_code",
            "redirect_uri": redirect, "code": code.removesuffix("#_")}), self.label)
        short = answer["data"][0] if isinstance(answer.get("data"), list) and answer["data"] else answer
        if not short.get("access_token"):
            raise ClippyError(key="error.platform_rejected", platform=self.label, detail="no access token")
        long = checked(http.get(EXCHANGE, timeout=TIMEOUT, params={
            "grant_type": "ig_exchange_token", "client_secret": secret, "access_token": short["access_token"]}),
            self.label)
        return self.connect(http, long.get("access_token") or short["access_token"], now,
                            float(long.get("expires_in") or LASTS))

    def needs_refresh(self, tokens: Tokens, now: float) -> bool:
        return tokens.expires_at - now < REFRESH_WHEN_LEFT

    def refresh(self, http, tokens: Tokens, now: float) -> Tokens:
        data = checked(http.get(REFRESH, params={"grant_type": "ig_refresh_token", "access_token": tokens.access_token},
                                timeout=TIMEOUT), self.label)
        return replace(tokens, access_token=data.get("access_token") or tokens.access_token,
                       expires_at=now + float(data.get("expires_in") or LASTS))

    def publish(self, http, tokens: Tokens, upload: Upload, post: PostText, options: dict) -> PostResult:
        check_length(self.label, upload, LONGEST)
        auth = {"access_token": tokens.access_token}
        owner = tokens.user_id or "me"
        container = checked(http.post(f"{GRAPH}/{owner}/media", timeout=TIMEOUT, params=auth, data={
            "media_type": "REELS", "upload_type": "resumable", "caption": post.caption[:CAPTION],
            "share_to_feed": "true"}), self.label)["id"]
        size = upload.size
        with Reader(upload.path) as body:
            checked(http.post(f"{RUPLOAD}/{container}", data=body, timeout=UPLOAD_TIMEOUT, headers={
                "Authorization": f"OAuth {tokens.access_token}", "offset": "0", "file_size": str(size)}), self.label)
        self._wait(http, container, auth)
        media = checked(http.post(f"{GRAPH}/{owner}/media_publish", params=auth, data={"creation_id": container},
                                  timeout=TIMEOUT), self.label)["id"]
        link = checked(http.get(f"{GRAPH}/{media}", params={**auth, "fields": "permalink"}, timeout=TIMEOUT),
                       self.label).get("permalink") or ""
        report(1.0)
        return PostResult(url=link, remote_id=str(media), state="public")

    def _wait(self, http, container: str, auth: dict) -> None:
        def finished() -> bool:
            state = checked(http.get(f"{GRAPH}/{container}", params={**auth, "fields": "status_code,status"},
                                     timeout=TIMEOUT), self.label)
            code = state.get("status_code")
            if code in ("ERROR", "EXPIRED"):
                raise ClippyError(key="error.platform_rejected", platform=self.label,
                                  detail=str(state.get("status") or code)[:200])
            return code == "FINISHED"

        self.wait_for(finished)
