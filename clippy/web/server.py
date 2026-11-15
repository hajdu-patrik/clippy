"""FastAPI wiring: REST for chats/messages, SSE for live progress, Range-capable media for clip previews."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import tempfile
import threading
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field, StringConstraints

from .. import i18n
from ..editing.assets import LIMITS
from ..tools import ClippyError
from ..llm import create_llm
from ..storage import Storage
from .chats import ChatService, NotFound
from .clips import ClipLibrary, ClipPresenter
from .db import DB
from .hub import Hub
from .jobs import Runner
from .media import MediaStore
from .lan import Lan, is_loopback
from .security import SecurityMiddleware

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


class NewMessage(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    attachments: list[AssetId] = Field(default_factory=list, max_length=10)


def error_detail(exc: ClippyError) -> dict:
    return {"message": str(exc), "key": exc.key, "params": {k: str(v) for k, v in exc.params.items()}}


class ChatPatch(BaseModel):
    title: str = Field(max_length=200)


def create_app(storage: Storage | None = None, db_path: Path | None = None, runner: Runner | None = None,
               llm_factory=None, start_worker: bool = True, lan: Lan | None = None) -> FastAPI:
    storage = storage or Storage()
    db = DB(db_path or storage.web / "clippy.db")
    hub = runner.hub if runner is not None else Hub()
    media = MediaStore(storage)
    presenter, library = ClipPresenter(media), ClipLibrary(storage)
    runner = runner or Runner(db, hub, storage, presenter, library, start=start_worker)
    chats = ChatService(db, hub, runner, storage, presenter, library, llm_factory or create_llm)
    if start_worker:
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

    @app.get("/api/i18n")
    def catalog():
        web = {lang: {k: v for k, v in texts.items() if k.split(".")[0] not in ("cli", "cmd", "flag", "help")}
               for lang, texts in i18n.CATALOG.items()}
        return {**web, "default": i18n.saved_language()}

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
