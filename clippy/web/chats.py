"""Chat use cases: create, rename, view, delete (with every file of the chat), and handle a user message."""

from __future__ import annotations

from typing import Callable

from ..concurrency import SOURCE
from .. import i18n
from ..download import require_public_url
from ..editing.assets import Asset, AssetService
from ..editing.repository import TimelineRepository
from ..llm import LLM
from ..storage import Storage
from ..tools import ClippyError
from . import agent
from .clips import ClipLibrary, ClipPresenter
from .hub import Hub
from .jobs import PLACEHOLDER_TITLE, Runner

NEW_TITLE = "New chat"


class NotFound(LookupError):
    pass


class ChatService:
    def __init__(self, db, hub: Hub, runner: Runner, storage: Storage, presenter: ClipPresenter,
                 library: ClipLibrary, llm_factory: Callable[[], LLM], assets: AssetService | None = None):
        self.db, self.hub, self.runner, self.storage = db, hub, runner, storage
        self.presenter, self.library, self.llm_factory = presenter, library, llm_factory
        self.assets = assets or AssetService(storage)

    def get(self, chat_id: int) -> dict:
        chat = self.db.get_chat(chat_id)
        if chat is None:
            raise NotFound(chat_id)
        return chat

    def create(self) -> dict:
        chat = self.db.create_chat(NEW_TITLE)
        self.hub.publish(chat["id"], {"type": "chat", "chat": chat})
        return chat

    def rename(self, chat_id: int, title: str) -> dict:
        self.get(chat_id)
        chat = self.db.update_chat(chat_id, title=title.strip()[:120] or NEW_TITLE)
        self.hub.publish(chat_id, {"type": "chat", "chat": chat})
        return chat

    def delete(self, chat_id: int) -> None:
        """The chat, its versions, scratch files and its copies in the video library go; the source cache goes too
        unless another chat uses it."""
        chat = self.get(chat_id)
        self.db.delete_chat(chat_id)
        self.hub.publish(chat_id, {"type": "chat_deleted"})
        self.runner.forget(chat_id)
        self.library.remove_chat(chat_id)
        if not self.runner.is_running(chat_id):
            self.storage.purge_chat(chat_id)
        stem = chat.get("video_id")
        if stem:
            with SOURCE.hold(stem):
                if not self.db.video_in_use(stem) and stem not in self.runner.sources_in_use():
                    self.storage.purge_source(stem)

    def view(self, chat_id: int) -> dict:
        chat = self.get(chat_id)
        messages = self.db.list_messages(chat_id)
        current = {c["id"] for c in self.db.current_clips(chat_id)}
        clips = {}
        for clip_id in {i for m in messages if m["kind"] == "clips" for i in m["data"].get("clip_ids", [])}:
            row = self.db.get_clip(clip_id)
            if row:
                clips[clip_id] = self.presenter.present(row, current=row["id"] in current)
        return {"chat": chat, "messages": messages, "clips": clips, "status": self.runner.status()}

    @staticmethod
    def present_asset(chat_id: int, asset: Asset) -> dict:
        return {"id": asset.id, "kind": asset.kind.value, "name": asset.name, "duration": asset.duration,
                "width": asset.width, "height": asset.height, "url": f"/media/{int(chat_id)}/assets/{asset.id}"}

    def upload(self, chat_id: int, name: str, stream) -> dict:
        self.get(chat_id)
        return self.present_asset(chat_id, self.assets.add(chat_id, name, stream))

    def list_assets(self, chat_id: int) -> list[dict]:
        self.get(chat_id)
        return [self.present_asset(chat_id, a) for a in self.assets.list(chat_id)]

    def remove_asset(self, chat_id: int, asset_id: str) -> bool:
        self.get(chat_id)
        return self.assets.remove(chat_id, asset_id)

    def asset_file(self, chat_id: int, asset_id: str):
        self.get(chat_id)
        asset = self.assets.index(chat_id).find(asset_id)
        return asset.path if asset is not None and asset.path.is_file() else None

    def timeline(self, chat_id: int, number: int) -> dict:
        chat = self.get(chat_id)
        row = next((c for c in self.db.current_clips(chat_id) if c["number"] == number), None)
        if row is None:
            raise NotFound(number)
        return TimelineRepository(self.db).of(row, chat.get("options") or {}).model_dump(mode="json")

    def undo(self, chat_id: int, number: int) -> dict:
        self.get(chat_id)
        if not any(c["number"] == number for c in self.db.current_clips(chat_id)):
            raise NotFound(number)
        job = self.runner.submit(chat_id, "undo", {"clips": [number]})
        return {"job": job.id}

    def post_message(self, chat_id: int, text: str, attachments: tuple[str, ...] = ()) -> dict:
        chat = self.get(chat_id)
        history = self.db.list_messages(chat_id)
        index = self.assets.index(chat_id)
        attached = [self.present_asset(chat_id, a) for a in (index.find(i) for i in dict.fromkeys(attachments)) if a]
        user_msg = self._say(chat_id, "user", text, data={"attachments": attached} if attached else None)
        if chat["title"] == NEW_TITLE:
            chat = self.db.update_chat(chat_id, title=PLACEHOLDER_TITLE if agent.URL_RE.search(text) else text[:60])
            self.hub.publish(chat_id, {"type": "chat", "chat": chat})
        project = self.runner.projects.get(chat_id)
        excerpt = project.transcript_text if project is not None and project.transcript is not None else None
        self.hub.publish(chat_id, {"type": "thinking", "on": True})
        try:
            result = agent.run_agent(self.llm_factory(), chat, self.db.current_clips(chat_id), history, text, excerpt,
                                     tuple(attached))
        finally:
            self.hub.publish(chat_id, {"type": "thinking", "on": False})
        notes = list(result["notes"])
        actions, jobs = [], []
        for action in result["actions"]:
            try:
                jobs.extend(self._execute(chat, action))
                actions.append(action)
            except ClippyError as exc:
                notes.append(str(exc))
        prefix = i18n.t(result["lang"], "note.prefix")
        reply = result["reply"] + (f"\n\n_{prefix}: " + "; ".join(notes) + "_" if notes else "")
        assistant = self._say(chat_id, "assistant", reply, data={"actions": actions})
        for kind, action in jobs:
            self.runner.submit(chat_id, kind, action)
        return {"user": user_msg, "assistant": assistant, "actions": actions}

    def _say(self, chat_id: int, role: str, text: str, data: dict | None = None) -> dict:
        msg = self.db.add_message(chat_id, role, text, data=data)
        self.hub.publish(chat_id, {"type": "message", "message": msg})
        return msg

    def _execute(self, chat: dict, action: dict) -> list[tuple[str, dict]]:
        """Instant actions run now; background jobs are validated and returned, to be queued after the reply."""
        kind = action["type"]
        if kind == "delete":
            self.db.mark_deleted(chat["id"], action["clip"])
            self.library.unpublish(chat, action["clip"])
            self.hub.publish(chat["id"], {"type": "deleted", "number": action["clip"]})
            return []
        if kind == "generate":
            require_public_url(action["url"])
        return [(kind, action)]
