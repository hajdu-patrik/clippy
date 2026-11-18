"""FastAPI wiring: REST for chats/messages, SSE for live progress, Range-capable media for clip previews."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field, StringConstraints

from .. import categories, i18n, streamers
from ..editing.assets import LIMITS
from ..tools import ClippyError, log
from ..download import sweep_partials
from ..llm import create_llm
from ..publish.registry import LABELS, PLATFORMS, POSTING
from ..publish.oauth import OAuthFlows
from ..settings import Settings
from ..storage import Storage
from .chats import ChatService, NotFound
from .clips import ClipLibrary, ClipPresenter, rewrite_post_texts
from .db import DB
from .hub import Hub
from .jobs import Runner
from .media import MediaStore
from .lan import Lan, is_loopback
from .security import SecurityMiddleware
from . import results
from .kit import StyleKit
from .daily import Daily, daily_settings, present_daily
from .watch import EVERY_MINUTES, Watcher, present_watch, watch_settings

STATIC = Path(__file__).resolve().parent / "static"


def static_build(folder: Path = STATIC) -> str:
    """A hash of every static file: it goes into the static URLs, so a change gives new URLs and no stale cache."""
    digest = hashlib.sha256()
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        digest.update(path.relative_to(folder).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def index_html(build: str) -> str:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return re.sub(r"""(["'])/static/""", rf"\1/static/{build}/", html)


AssetId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]{6,16}$")]
MAX_UPLOAD = max(LIMITS.values())
SPOOL_BYTES = 16 * 1024 * 1024
DICTATION_BYTES = 8 * 1024 * 1024


class NewMessage(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    attachments: list[AssetId] = Field(default_factory=list, max_length=10)


def error_detail(exc: ClippyError) -> dict:
    return {"message": str(exc), "key": exc.key, "params": {k: str(v) for k, v in exc.params.items()}}


class ChatPatch(BaseModel):
    title: str = Field(max_length=200)


class SettingsPatch(BaseModel):
    profile_links: str = Field(max_length=2000)


Platform = Literal[PLATFORMS]
PostingPlatform = Literal[POSTING]


class PublishRequest(BaseModel):
    platforms: list[PostingPlatform] = Field(min_length=1, max_length=len(POSTING))
    youtube_privacy: Literal["public", "unlisted", "private"] = "public"
    tiktok_mode: Literal["draft", "private"] = "draft"
    variant: Literal["a", "b"] = "a"
    at: float | None = Field(default=None, gt=0)


class NewWatch(BaseModel):
    url: str = Field(min_length=8, max_length=500)
    n: int = Field(default=0, ge=0, le=12)
    preset: str = Field(default="", max_length=20)
    instructions: str = Field(default="", max_length=400)
    every: int = 60
    platforms: list[PostingPlatform] = Field(default_factory=list, max_length=len(POSTING))
    youtube_privacy: Literal["public", "unlisted", "private"] = "public"
    tiktok_mode: Literal["draft", "private"] = "draft"
    slots: str = Field(default="", max_length=120)
    latest: bool = False


class WatchPatch(BaseModel):
    enabled: bool


class NewDaily(BaseModel):
    game: str = Field(min_length=2, max_length=40)
    n: int = Field(default=3, ge=1, le=5)
    at: str = Field(default="09:00", max_length=5)
    times: str = Field(default="", max_length=80)
    now: bool = True


class KitPatch(BaseModel):
    font: str = Field(default="", max_length=40)
    highlight: str = Field(default="", max_length=20)
    uppercase: bool | None = None
    position: str = Field(default="top-right", max_length=20)
    width: float = Field(default=0.22, ge=0.05, le=0.5)


class InstagramToken(BaseModel):
    token: str = Field(min_length=20, max_length=2000)


def create_app(storage: Storage | None = None, db_path: Path | None = None, runner: Runner | None = None,
               llm_factory=None, start_worker: bool = True, lan: Lan | None = None) -> FastAPI:
    storage = storage or Storage()
    db = DB(db_path or storage.web / "clippy.db")
    hub = runner.hub if runner is not None else Hub()
    media = MediaStore(storage)
    presenter, library = ClipPresenter(media, db), ClipLibrary(storage)
    runner = runner or Runner(db, hub, storage, presenter, library, start=start_worker)
    chats = ChatService(db, hub, runner, storage, presenter, library, llm_factory or create_llm)
    watcher = Watcher(db, runner, hub)
    watcher.daily = Daily(db, runner, hub)
    chats.watcher = watcher
    if start_worker:
        watcher.start()
        sweep_partials(storage.cache("downloads"))
        threading.Thread(target=lambda: (llm_factory or create_llm)().warm(), name="clippy-llm-warmup",
                         daemon=True).start()

    build = static_build()
    index_page = index_html(build)
    app = FastAPI(title="Clippy", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SecurityMiddleware, build=build, lan=lan)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.state.db, app.state.hub, app.state.runner, app.state.chats = db, hub, runner, chats
    app.mount(f"/static/{build}", StaticFiles(directory=STATIC), name="static-built")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def found(call, *args):
        try:
            return call(*args)
        except NotFound:
            raise HTTPException(404, "No such chat") from None

    @app.get("/")
    def index():
        return HTMLResponse(index_page)

    @app.get("/favicon.ico")
    def favicon():
        return FileResponse(STATIC / "brand" / "favicon.ico", media_type="image/x-icon")

    @app.get("/api/status")
    def status():
        return {**runner.status(), "lan": lan is not None}

    def lan_from_this_computer(request: Request) -> Lan:
        if lan is None or not is_loopback(request.client.host if request.client else ""):
            raise HTTPException(404, "Not found")
        return lan

    @app.post("/api/lan/pair")
    def lan_pair(request: Request):
        return lan_from_this_computer(request).new_code()

    @app.post("/api/lan/forget")
    def lan_forget(request: Request):
        return {"forgotten": lan_from_this_computer(request).forget_all()}

    settings = Settings(storage)
    accounts, flows, notices = runner.accounts, OAuthFlows(), {}

    def from_this_computer(request: Request) -> None:
        if not is_loopback(request.client.host if request.client else ""):
            raise HTTPException(403, error_detail(ClippyError(key="error.accounts_here_only")))

    def accounts_changed() -> None:
        hub.publish(None, {"type": "accounts"})

    def redirect_uri(request: Request, platform: str) -> str:
        return f"http://127.0.0.1:{request.url.port or 80}/oauth/{platform}/callback"

    @app.get("/api/accounts")
    def get_accounts(request: Request):
        return {"accounts": accounts.status(), "notices": notices,
                "here": is_loopback(request.client.host if request.client else "")}

    @app.post("/api/accounts/{platform}/connect")
    def connect_account(platform: Platform, request: Request):
        """The sign-in page's address; the browser goes there and the platform sends it back to the callback."""
        from_this_computer(request)
        target = accounts.platform(platform)
        if not target.oauth:
            raise HTTPException(400, "Paste the token instead")
        try:
            state, verifier = flows.start(platform)
            return {"url": target.auth_url(redirect_uri(request, platform), state, verifier)}
        except ClippyError as exc:
            raise HTTPException(400, error_detail(exc)) from None

    @app.get("/oauth/{platform}/callback")
    def oauth_callback(platform: Platform, request: Request, code: str = "", state: str = "", error: str = ""):
        from_this_computer(request)
        verifier = flows.finish(platform, state)
        notices.pop(platform, None)
        try:
            if error:
                raise ClippyError(key="error.oauth_denied", platform=LABELS[platform])
            if verifier is None or not code:
                raise ClippyError(key="error.oauth_state", platform=LABELS[platform])
            tokens = accounts.platform(platform).exchange(accounts.http, code, verifier,
                                                          redirect_uri(request, platform), time.time())
            accounts.save(platform, tokens)
        except ClippyError as exc:
            notices[platform] = {"key": exc.key, "params": {k: str(v) for k, v in exc.params.items()}}
        except Exception as exc:
            notices[platform] = {"key": "error.platform_network", "params": {"platform": LABELS[platform]}}
            log(f"[publish] sign-in failed: {type(exc).__name__}: {exc}")
        accounts_changed()
        return RedirectResponse("/#settings", status_code=303)

    @app.put("/api/accounts/instagram")
    def connect_instagram(body: InstagramToken, request: Request):
        from_this_computer(request)
        try:
            tokens = accounts.platform("instagram").connect(accounts.http, body.token, time.time())
        except ClippyError as exc:
            raise HTTPException(400, error_detail(exc)) from None
        except Exception:
            raise HTTPException(400, error_detail(ClippyError(key="error.platform_network",
                                                              platform="Instagram"))) from None
        accounts.save("instagram", tokens)
        notices.pop("instagram", None)
        accounts_changed()
        return accounts.status()["instagram"]

    @app.delete("/api/accounts/{platform}")
    def disconnect_account(platform: Platform, request: Request):
        from_this_computer(request)
        accounts.remove(platform)
        notices.pop(platform, None)
        accounts_changed()
        return {"ok": True}

    @app.get("/api/settings")
    def get_settings():
        return settings.load()

    @app.put("/api/settings")
    def put_settings(body: SettingsPatch):
        try:
            saved = settings.set_profile_links(body.profile_links)
        except ClippyError as exc:
            raise HTTPException(400, error_detail(exc)) from None
        rewrite_post_texts(db, runner.library, saved["profile_links"])
        hub.publish(None, {"type": "settings", "settings": saved})
        return saved

    @app.get("/api/i18n")
    def catalog():
        return {k: v for k, v in i18n.CATALOG.items() if k.split(".")[0] not in ("cli", "cmd", "flag", "help")}

    @app.get("/api/chats")
    def list_chats():
        return db.list_chats()

    @app.post("/api/chats")
    def new_chat():
        return chats.create()

    @app.patch("/api/chats/{chat_id}")
    def rename_chat(chat_id: int, body: ChatPatch):
        return found(chats.rename, chat_id, body.title)

    @app.delete("/api/chats/{chat_id}")
    def delete_chat(chat_id: int):
        found(chats.delete, chat_id)
        return {"ok": True}

    @app.get("/api/chats/{chat_id}")
    def get_chat(chat_id: int):
        return found(chats.view, chat_id)

    @app.post("/api/chats/{chat_id}/messages")
    def post_message(chat_id: int, body: NewMessage):
        if not body.text.strip():
            raise HTTPException(400, "Empty message")
        return found(chats.post_message, chat_id, body.text.strip(), tuple(body.attachments))

    @app.put("/api/chats/{chat_id}/assets")
    async def upload_asset(chat_id: int, request: Request, name: str = Query(min_length=1, max_length=200)):
        found(chats.get, chat_id)
        with tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES) as spool:
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, "File too large")
                spool.write(chunk)
            spool.seek(0)
            try:
                return await run_in_threadpool(chats.upload, chat_id, name, spool)
            except ClippyError as exc:
                raise HTTPException(400, error_detail(exc)) from None

    @app.get("/api/chats/{chat_id}/assets")
    def list_assets(chat_id: int):
        return found(chats.list_assets, chat_id)

    @app.delete("/api/chats/{chat_id}/assets/{asset_id}")
    def delete_asset(chat_id: int, asset_id: AssetId):
        if not found(chats.remove_asset, chat_id, asset_id):
            raise HTTPException(404, "No such attachment")
        return {"ok": True}

    @app.get("/api/chats/{chat_id}/clips/{number}/timeline")
    def clip_timeline(chat_id: int, number: int):
        return found(chats.timeline, chat_id, number)

    @app.post("/api/chats/{chat_id}/clips/{number}/undo")
    def undo_clip(chat_id: int, number: int):
        return found(chats.undo, chat_id, number)

    @app.post("/api/chats/{chat_id}/clips/{number}/publish")
    def publish_clip(chat_id: int, number: int, body: PublishRequest):
        status = accounts.status()
        missing = [p for p in body.platforms if not status[p]["connected"]]
        if missing:
            raise HTTPException(400, error_detail(ClippyError(key="error.account_missing", platform=LABELS[missing[0]])))
        options = {"youtube": {"privacy": body.youtube_privacy}, "tiktok": {"mode": body.tiktok_mode},
                   **({"variant": "b"} if body.variant == "b" else {})}
        try:
            return found(chats.publish, chat_id, number, list(dict.fromkeys(body.platforms)), options, body.at)
        except ClippyError as exc:
            raise HTTPException(400, error_detail(exc)) from None

    @app.delete("/api/scheduled/{item_id}")
    def cancel_scheduled(item_id: int):
        if not chats.cancel_scheduled(item_id):
            raise HTTPException(404, "Not planned")
        return {"ok": True}

    kit = StyleKit(storage)

    @app.post("/api/dictate")
    async def dictate(request: Request):
        """Voice input: a short recording in, its words out (the small Whisper model on the CPU)."""
        from ..dictation import dictate as words_of

        with tempfile.TemporaryDirectory(prefix="clippy-voice-") as folder:
            path = Path(folder) / "voice.webm"
            size = 0
            with path.open("wb") as out:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > DICTATION_BYTES:
                        raise HTTPException(413, "Recording too long")
                    out.write(chunk)
            try:
                return {"text": await run_in_threadpool(words_of, path)}
            except ClippyError as exc:
                raise HTTPException(400, error_detail(exc)) from None

    @app.get("/api/kit")
    def get_kit():
        return kit.present()

    @app.put("/api/kit")
    def put_kit(body: KitPatch):
        kit.save(body.font, body.highlight, body.uppercase, body.position, body.width)
        return kit.present()

    @app.put("/api/kit/{slot}")
    async def put_kit_file(slot: Literal["logo", "intro", "outro"], request: Request,
                           name: str = Query(min_length=1, max_length=200)):
        with tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES) as spool:
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, "File too large")
                spool.write(chunk)
            spool.seek(0)
            try:
                await run_in_threadpool(kit.put, slot, name, spool)
            except ClippyError as exc:
                raise HTTPException(400, error_detail(exc)) from None
        return kit.present()

    @app.delete("/api/kit/{slot}")
    def delete_kit_file(slot: Literal["logo", "intro", "outro"]):
        kit.remove(slot)
        return kit.present()

    @app.get("/api/results")
    def get_results():
        status = accounts.status()
        return {**results.insights(db), "analytics": status["youtube"].get("analytics", True)}

    @app.post("/api/results/refresh")
    def refresh_results():
        return {"read": watcher.measure(), **results.insights(db)}

    @app.get("/api/streamers")
    def list_streamers():
        return {"streamers": streamers.listing()}

    @app.get("/api/watches")
    def list_watches():
        return {"watches": [present_watch(w) for w in db.list_watches()], "every": list(EVERY_MINUTES)}

    @app.post("/api/watches")
    def add_watch(body: NewWatch):
        try:
            settings = watch_settings(body.n, body.preset, body.instructions, body.every, body.platforms,
                                      body.youtube_privacy, body.tiktok_mode, body.slots)
            return present_watch(watcher.add(body.url, settings, body.latest))
        except ClippyError as exc:
            raise HTTPException(400, error_detail(exc)) from None

    @app.patch("/api/watches/{watch_id}")
    def toggle_watch(watch_id: int, body: WatchPatch):
        if db.get_watch(watch_id) is None:
            raise HTTPException(404, "No such watch")
        watch = db.update_watch(watch_id, enabled=int(body.enabled))
        watcher.changed()
        return present_watch(watch)

    @app.post("/api/watches/{watch_id}/check")
    def check_watch(watch_id: int):
        watch = db.get_watch(watch_id)
        if watch is None:
            raise HTTPException(404, "No such watch")
        return {"new": watcher.check(watch)}

    @app.delete("/api/watches/{watch_id}")
    def delete_watch(watch_id: int):
        if not db.delete_watch(watch_id):
            raise HTTPException(404, "No such watch")
        watcher.changed()
        return {"ok": True}

    @app.get("/api/daily")
    def list_daily():
        return {"daily": [present_daily(db, presenter, d) for d in db.list_daily()],
                "games": [{"key": g.key, "name": g.name} for g in categories.GAMES]}

    @app.post("/api/daily")
    def add_daily(body: NewDaily):
        try:
            d = watcher.daily.add(body.game, daily_settings(body.n, body.at, body.times), body.now)
        except ClippyError as exc:
            raise HTTPException(400, error_detail(exc)) from None
        return present_daily(db, presenter, d)

    @app.patch("/api/daily/{daily_id}")
    def toggle_daily(daily_id: int, body: WatchPatch):
        if db.get_daily(daily_id) is None:
            raise HTTPException(404, "No such daily pick")
        d = db.update_daily(daily_id, enabled=int(body.enabled))
        watcher.daily.changed()
        return present_daily(db, presenter, d)

    @app.post("/api/daily/{daily_id}/run")
    def run_daily(daily_id: int):
        d = db.get_daily(daily_id)
        if d is None:
            raise HTTPException(404, "No such daily pick")
        return {"new": watcher.daily.run(d)}

    @app.delete("/api/daily/{daily_id}")
    def delete_daily(daily_id: int):
        if not db.delete_daily(daily_id):
            raise HTTPException(404, "No such daily pick")
        watcher.daily.changed()
        return {"ok": True}

    @app.get("/api/events")
    async def events(request: Request):
        """One stream per browser (the windows share it): every chat's events, tagged with the chat id. It starts
        with "hello", so a window that reconnects knows it may have missed something and reloads."""
        q = hub.subscribe()

        async def stream():
            try:
                hello = {"type": "hello", "chat_id": None, "status": runner.status()}
                yield f"data: {json.dumps(hello, ensure_ascii=False)}\n\n"
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                hub.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/media/{chat_id}/assets/{asset_id}")
    def asset_media(chat_id: int, asset_id: AssetId):
        target = found(chats.asset_file, chat_id, asset_id)
        if target is None:
            raise HTTPException(404, "No such file")
        return FileResponse(target)

    @app.get("/media/{chat_id}/{name}")
    def clip_media(chat_id: int, name: str):
        target = media.resolve(chat_id, name)
        if target is None:
            raise HTTPException(404, "No such file")
        return FileResponse(target)

    return app
