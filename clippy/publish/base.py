"""What every account type shares: one class per platform fills in its endpoints, scopes and, where the platform
allows it, its upload. The sign-in round trip, the token refresh and the clear errors live here once."""

from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode

from ..tools import ClippyError
from . import TIMEOUT, PostResult, PostText, Tokens, Upload, checked
from .oauth import s256_challenge

REFRESH_WHEN_LEFT = 300
POLL_SECONDS, POLL_TRIES = 5, 120


class Platform:
    """One card in Settings: whether it is set up, how it signs in and whether Clippy can post to it. The developer
    app's keys come from the card's setup (`keys`, an AppKeys) or else from .env (`env`, in the order of `fields`)."""

    name = label = ""
    oauth = True
    posting = True
    schedules = False
    renews = False
    pasteable = False
    console = ""
    env: tuple[str, ...] = ()
    fields: tuple[str, ...] = ("client_id", "client_secret")
    optional: dict[str, str] = {}
    keys = None

    def __init__(self, sleep=time.sleep):
        self.sleep = sleep

    def wait_for(self, finished) -> None:
        """Asks `finished()` every few seconds until it says yes; a platform that never gets there is too slow."""
        for _ in range(POLL_TRIES):
            if finished():
                return
            self.sleep(POLL_SECONDS)
        raise ClippyError(key="error.platform_slow", platform=self.label)

    def key(self, field: str, env_name: str) -> str:
        stored = self.keys.get(self.name) if self.keys is not None else {}
        return str(stored.get(field) or os.environ.get(env_name) or "").strip()

    def client(self) -> tuple[str, ...]:
        """The developer app's required keys; missing ones are a clear error that points to the card's setup."""
        values = tuple(self.key(f, e) for f, e in zip(self.fields, self.env))
        if not values or not all(values):
            raise ClippyError(key="error.platform_not_configured", platform=self.label)
        return values

    def form(self) -> list[dict]:
        """The setup fields the card shows, and whether each one is already set (never its value)."""
        pairs = [(f, e, False) for f, e in zip(self.fields, self.env)] + [(f, e, True) for f, e in self.optional.items()]
        return [{"name": f, "secret": "secret" in f, "optional": opt, "set": bool(self.key(f, e))} for f, e, opt in pairs]

    def configured(self) -> bool:
        try:
            self.client()
        except ClippyError:
            return False
        return True

    def needs_refresh(self, tokens: Tokens, now: float) -> bool:
        return bool(tokens.expires_at) and tokens.expires_at - now < REFRESH_WHEN_LEFT

    def refresh(self, http, tokens: Tokens, now: float) -> Tokens:
        raise ClippyError(key="error.account_expired", platform=self.label)

    def publish(self, http, tokens: Tokens, upload: Upload, post: PostText, options: dict) -> PostResult:
        raise ClippyError(key="error.posting_unsupported", platform=self.label)

    def set_cover(self, http, tokens: Tokens, remote_id: str, image: Path) -> bool:
        """Sets a posted clip's cover afterwards; False when the platform has no separate cover or turned it down."""
        return False


class OAuthPlatform(Platform):
    """Authorization code sign-in for a desktop app: a state, PKCE where the platform has it, then the tokens."""

    auth_endpoint = token_url = ""
    scopes = ""
    scope_env = ""
    id_param, secret_param = "client_id", "client_secret"
    challenge = staticmethod(s256_challenge)
    lifetime = 3600
    extra_auth: dict = {}

    def scope(self) -> str:
        return (os.environ.get(self.scope_env) if self.scope_env else "") or self.scopes

    def auth_params(self, params: dict) -> dict:
        return params

    def auth_url(self, redirect: str, state: str, verifier: str) -> str:
        params = {self.id_param: self.client()[0], "redirect_uri": redirect, "response_type": "code",
                  "scope": self.scope(), "state": state, **self.extra_auth}
        if self.challenge:
            params |= {"code_challenge": self.challenge(verifier), "code_challenge_method": "S256"}
        return self.auth_endpoint + "?" + urlencode(self.auth_params(params))

    def client_auth(self) -> tuple[dict, dict]:
        """The app's credentials for a token request: (form fields, headers)."""
        client_id, secret = self.client()
        return {self.id_param: client_id, self.secret_param: secret}, {}

    def token_call(self, http, form: dict) -> dict:
        fields, headers = self.client_auth()
        data = checked(http.post(self.token_url, data={**fields, **form}, headers=headers, timeout=TIMEOUT),
                       self.label)
        if not data.get("access_token"):
            raise ClippyError(key="error.platform_rejected", platform=self.label, detail="no access token")
        return data

    def tokens_of(self, data: dict, now: float) -> Tokens:
        scope = data.get("scope") or ""
        return Tokens(access_token=data["access_token"], refresh_token=data.get("refresh_token") or "",
                      expires_at=now + float(data.get("expires_in") or self.lifetime),
                      scope=" ".join(scope) if isinstance(scope, list) else str(scope))

    def identify(self, http, tokens: Tokens) -> None:
        """Fills in the account's name (and id) so Settings can show who is connected."""

    def exchange(self, http, code: str, verifier: str, redirect: str, now: float) -> Tokens:
        form = {"code": code, "grant_type": "authorization_code", "redirect_uri": redirect}
        if self.challenge:
            form["code_verifier"] = verifier
        tokens = self.tokens_of(self.token_call(http, form), now)
        self.identify(http, tokens)
        return tokens

    def refresh(self, http, tokens: Tokens, now: float) -> Tokens:
        if not tokens.refresh_token:
            raise ClippyError(key="error.account_expired", platform=self.label)
        try:
            data = self.token_call(http, {"grant_type": "refresh_token", "refresh_token": tokens.refresh_token})
        except ClippyError as exc:
            if exc.key == "error.platform_rejected":
                raise ClippyError(key="error.account_expired", platform=self.label) from None
            raise
        return replace(tokens, access_token=data["access_token"],
                       expires_at=now + float(data.get("expires_in") or self.lifetime),
                       refresh_token=data.get("refresh_token") or tokens.refresh_token)
