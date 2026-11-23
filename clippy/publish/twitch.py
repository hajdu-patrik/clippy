"""Twitch: the sign-in works, posting does not. Twitch's public API has no endpoint that uploads a video, and
Clippy uses no private one, so the account is connected only to show whose channel it is."""

from __future__ import annotations

from . import TIMEOUT, Tokens, bearer, checked
from .base import OAuthPlatform

AUTH = "https://id.twitch.tv/oauth2/authorize"
TOKEN = "https://id.twitch.tv/oauth2/token"
API = "https://api.twitch.tv/helix"


class Twitch(OAuthPlatform):
    name, label, posting = "twitch", "Twitch", False
    env = ("CLIPPY_TWITCH_CLIENT_ID", "CLIPPY_TWITCH_CLIENT_SECRET")
    console = "https://dev.twitch.tv/console/apps"
    auth_endpoint, token_url, scopes, scope_env = AUTH, TOKEN, "user:read:email", "CLIPPY_TWITCH_SCOPES"
    challenge = None
    lifetime = 14400

    def identify(self, http, tokens: Tokens) -> None:
        me = checked(http.get(f"{API}/users", timeout=TIMEOUT, headers={
            **bearer(tokens.access_token), "Client-Id": self.client()[0]}), self.label)
        user = (me.get("data") or [{}])[0]
        tokens.name = str(user.get("display_name") or user.get("login") or "")
        tokens.user_id = str(user.get("id") or "")
