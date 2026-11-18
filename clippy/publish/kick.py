"""Kick: the sign-in works, posting does not. Kick's public API has no endpoint that uploads a video or a clip, and
Clippy uses no private one, so the account is connected only to show whose channel it is."""

from __future__ import annotations

from . import TIMEOUT, Tokens, bearer, checked
from .base import OAuthPlatform

AUTH = "https://id.kick.com/oauth/authorize"
TOKEN = "https://id.kick.com/oauth/token"
API = "https://api.kick.com/public/v1"


class Kick(OAuthPlatform):
    name, label, posting = "kick", "Kick", False
    env = ("CLIPPY_KICK_CLIENT_ID", "CLIPPY_KICK_CLIENT_SECRET")
    auth_endpoint, token_url, scopes = AUTH, TOKEN, "user:read channel:read"

    def identify(self, http, tokens: Tokens) -> None:
        me = checked(http.get(f"{API}/users", headers=bearer(tokens.access_token), timeout=TIMEOUT), self.label)
        user = (me.get("data") or [{}])[0]
        tokens.name = str(user.get("name") or "")
        tokens.user_id = str(user.get("user_id") or "")
