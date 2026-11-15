"""HTTP hardening for the local server: host allow-list (DNS rebinding), same-origin check plus a custom
header on state-changing requests (CSRF), and security headers with a strict Content-Security-Policy."""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qs

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import cookie_parser
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from .. import i18n
from .lan import COOKIE, COOKIE_AGE, Lan, is_loopback, is_private

ALLOWED_HOSTS = ("127.0.0.1", "localhost", "testserver")
CSRF_HEADER = "x-clippy-request"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
       "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'")
HEADERS = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}


def cache_policy(path: str, build: str) -> str:
    """Content-hashed static URLs never change, so they are cached for a year; everything else is revalidated."""
    if build and path.startswith(f"/static/{build}/"):
        return "public, max-age=31536000, immutable"
    if path.startswith("/media/"):
        return "private, max-age=86400"
    if path == "/favicon.ico":
        return "public, max-age=604800"
    if path.startswith("/api/"):
        return "no-store"
    return "no-cache"


def _host_of(value: str) -> str:
    host = value.split("://", 1)[-1].split("/", 1)[0]
    return host.rsplit(":", 1)[0] if not host.startswith("[") else host


PAIR_CODE = re.compile(r"[A-Za-z0-9_-]{8,64}")
PAIR_PAGE_FILES = ("/static/style.css", "/static/brand/mark.svg")


def pair_page(code: str) -> str:
    """The pairing confirmation in both languages; the code goes back with the button's POST."""
    t = {lang: {k: html.escape(i18n.t(lang, f"lan.page_{k}")) for k in ("title", "text", "button")}
         for lang in ("hu", "en")}
    return (f'<!doctype html><html lang="hu" data-theme="dark"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>Clippy</title>'
            f'<link rel="stylesheet" href="/static/style.css"></head><body class="pair-body"><main class="pair">'
            f'<img src="/static/brand/mark.svg" alt="" width="56" height="56">'
            f'<h1>{t["hu"]["title"]}</h1><p>{t["hu"]["text"]}</p><p lang="en">{t["en"]["text"]}</p>'
            f'<form method="post" action="/pair?code={html.escape(code)}">'
            f'<button class="pair-button" type="submit">{t["hu"]["button"]} · {t["en"]["button"]}</button>'
            f'</form></main></body></html>')


def _both(key: str) -> str:
    return "\n\n".join(i18n.t(lang, key) for lang in ("hu", "en"))


class SecurityMiddleware:
    """`lan`: requests from other devices are let in only from a private address with a paired device's cookie."""

    def __init__(self, app, build: str = "", lan: Lan | None = None):
        self.app, self.build, self.lan = app, build, lan
        self.hosts = ALLOWED_HOSTS + (lan.hosts if lan else ())

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                out = MutableHeaders(scope=message)
                for key, value in HEADERS.items():
                    out.setdefault(key, value)
                out["cache-control"] = cache_policy(scope["path"], self.build)
            await send(message)

        headers = Headers(scope=scope)
        pairing = self.lan is not None and scope["path"] == "/pair"
        problem = self._reject(scope["method"], headers, form=pairing)
        if problem is None and self.lan is not None:
            client = (scope.get("client") or ("", 0))[0]
            if not is_loopback(client):
                if not is_private(client):
                    problem = "Only the local network"
                elif pairing:
                    return await self._pair(scope, headers)(scope, receive, send_with_headers)
                elif not (scope["method"] == "GET" and scope["path"] in PAIR_PAGE_FILES) and not self.lan.known(
                        cookie_parser(headers.get("cookie", "")).get(COOKIE)):
                    problem = _both("lan.unpaired")
        if problem:
            return await PlainTextResponse(problem, status_code=403)(scope, receive, send_with_headers)
        await self.app(scope, receive, send_with_headers)

    def _pair(self, scope, headers: Headers):
        """GET shows a button, only its POST uses the code up: link previews of chat apps open links too."""
        code = (parse_qs(scope.get("query_string", b"").decode("latin-1")).get("code") or [""])[0]
        if not PAIR_CODE.fullmatch(code):
            return PlainTextResponse(_both("lan.bad_code"), status_code=403)
        if scope["method"] != "POST":
            return HTMLResponse(pair_page(code), headers={"referrer-policy": "same-origin"})
        token = self.lan.pair(code, headers.get("user-agent", ""))
        if token is None:
            return PlainTextResponse(_both("lan.bad_code"), status_code=403)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE, token, max_age=COOKIE_AGE, httponly=True, samesite="strict", path="/")
        return response

    def _reject(self, method: str, headers: Headers, form: bool = False) -> str | None:
        if _host_of(headers.get("host", "")) not in self.hosts:
            return "Invalid host"
        if method in SAFE_METHODS:
            return None
        origin = headers.get("origin")
        if origin and _host_of(origin) not in self.hosts:
            return "Cross-origin request blocked"
        if form:
            return None if origin else "Missing origin"
        if headers.get(CSRF_HEADER) != "1":
            return "Missing request header"
        return None
