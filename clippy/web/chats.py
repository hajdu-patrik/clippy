"""Chat use cases: create, rename, view, delete (with every file of the chat), and handle a user message."""

from __future__ import annotations

from typing import Callable

from ..concurrency import SOURCE
from .. import i18n, streamers
from ..download import require_public_url
from ..editing.assets import Asset, AssetService
from ..editing.repository import TimelineRepository
from ..llm import LLM
from ..publish.registry import LABELS, POSTING
from ..storage import Storage
from ..tools import ClippyError
from . import agent
from . import results, schedule
from .clips import ClipLibrary, ClipPresenter
from .hub import Hub
from .jobs import PLACEHOLDER_TITLE, Runner
from .daily import DEFAULT_AT, daily_settings
from .watch import generate_payload, watch_settings

NEW_TITLE = "New chat"


class NotFound(LookupError):
    pass


class ChatService:
    def __init__(self, db, hub: Hub, runner: Runner, storage: Storage, presenter: ClipPresenter,
                 library: ClipLibrary, llm_factory: Callable[[], LLM], assets: AssetService | None = None):
        self.watcher = None
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

    def publish(self, chat_id: int, number: int, platforms: list[str], options: dict, at: float | None = None) -> dict:
        """Now (a posting job) or at `at` (a planned post the watch loop starts on time)."""
        self.get(chat_id)
        row = next((c for c in self.db.current_clips(chat_id) if c["number"] == number), None)
        if row is None:
            raise NotFound(number)
        if at is not None:
            item = schedule.plan(self.db, chat_id, number, platforms, options, at)
            self.hub.publish(chat_id, {"type": "clip", "clip": self.presenter.present(row, current=True)})
            return {"scheduled": item["id"]}
        job = self.runner.submit(chat_id, "publish", {"clip": number, "platforms": platforms, "options": options})
        return {"job": job.id}

    def cancel_scheduled(self, item_id: int) -> bool:
        item = self.db.get_scheduled(item_id)
        if item is None or not self.db.set_scheduled_state(item_id, "cancelled"):
            return False
        row = next((c for c in self.db.current_clips(item["chat_id"]) if c["number"] == item["number"]), None)
        if row is not None:
            self.hub.publish(item["chat_id"], {"type": "clip", "clip": self.presenter.present(row, current=True)})
        return True

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
            learned = results.lessons(results.insights(self.db), ("hook", "length", "kind", "hour"))
            result = agent.run_agent(self.llm_factory(), chat, self.db.current_clips(chat_id), history, text, excerpt,
                                     tuple(attached), learned)
        finally:
            self.hub.publish(chat_id, {"type": "thinking", "on": False})
        notes = list(result["notes"])
        actions, jobs = [], []
        for action in result["actions"]:
            try:
                jobs.extend(self._execute(chat, action))
                actions.append(action)
            except ClippyError as exc:
                notes.append(i18n.t(exc.key, **exc.params) if exc.key else str(exc))
        prefix = i18n.t("note.prefix")
        picked = [i18n.t("reply.latest_found", **a["picked"]) for a in actions
                  if a["type"] == "latest_stream"]
        reply = ("\n\n".join([result["reply"], *picked])
                 + (f"\n\n_{prefix}: " + "; ".join(notes) + "_" if notes else ""))
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
        if kind == "publish":
            return self._publish_jobs(action)
        if kind == "latest_stream":
            return [("generate", self._latest_stream_job(chat, action))]
        if kind == "watch":
            self.watcher.add(action["url"], watch_settings(action["n"], action["preset"]))
            return []
        if kind == "daily":
            current = next((d["settings"] for d in self.db.list_daily() if d["game"] == action["game"]), {})
            self.watcher.daily.add(action["game"], daily_settings(action["n"], current.get("at") or DEFAULT_AT,
                                                                  current.get("times") or ""))
            return []
        return [(kind, action)]

    def _latest_stream_job(self, chat: dict, action: dict) -> dict:
        """The streamer's newest finished stream, clipped with the gameplay preset; `action` keeps what was picked."""
        found = streamers.latest_stream(streamers.Streamer(action["streamer"], (), **action["handles"]))
        require_public_url(found.url)
        action["picked"] = {"streamer": found.streamer, "platform": found.label, "title": found.title,
                            "date": found.date, "url": found.url}
        chat = self.db.update_chat(chat["id"], title=(found.title or found.streamer)[:80])
        self.hub.publish(chat["id"], {"type": "chat", "chat": chat})
        return generate_payload(found.url, watch_settings(action["n"], "gameplay"))

    def _publish_jobs(self, action: dict) -> list[tuple[str, dict]]:
        status = self.runner.accounts.status()
        connected = [p for p in POSTING if status[p]["connected"]]
        platforms = [p for p in action["platforms"] if p in connected] if action["platforms"] else connected
        if not platforms:
            missing = action["platforms"][0] if action["platforms"] else "youtube"
            raise ClippyError(key="error.account_missing", platform=LABELS[missing])
        action["platforms"] = platforms
        return [("publish", {"clip": n, "platforms": platforms, "options": action["options"]}) for n in action["clips"]]
