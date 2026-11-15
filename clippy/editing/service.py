"""Edits one stored clip: build its timeline, make sure the base exists, plan with the LLM, render the new version.

Storing the version and telling the chat about it is the caller's job (the web job), so this stays testable."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..highlights import Clip
from ..pipeline import post_text
from ..tools import atomic_write_text
from .assets import AssetService
from .changes import Change
from .operations import EditContext
from .planner import EditPlanner, EditRequest
from .render.base import BaseStore
from .render.finishing import FINAL
from .render.planner import TimelineRenderer
from .repository import TimelineRepository
from .timeref import TimeResolver, corrected


@dataclass
class NewVersion:
    """Everything needed to store the next version of a clip."""

    clip: dict
    info: dict
    mp4: str
    jpg: str
    txt: str
    timeline: str


@dataclass
class EditOutcome:
    number: int
    reply: str = ""
    changes: tuple[Change, ...] = ()
    question: str | None = None
    errors: list[str] = field(default_factory=list)
    notice: str | None = None
    version: NewVersion | None = None


class EditService:
    def __init__(self, db, storage, assets: AssetService, planner: EditPlanner, repo: TimelineRepository | None = None):
        self.db, self.storage, self.assets, self.planner = db, storage, assets, planner
        self.repo = repo or TimelineRepository(db)

    def edit(self, project, chat: dict, row: dict, request: str, attached: tuple[str, ...] = ()) -> EditOutcome:
        chat_id, number = chat["id"], row["number"]
        timeline = self.repo.of(row, chat.get("options") or {})
        renderer = TimelineRenderer(project, BaseStore(self.storage.chat_base(chat_id)))
        base, _ = renderer.base(timeline)
        captions = next(iter(timeline.layers_of("captions")), None)
        words = corrected(base.words, captions.fixes if captions else ())
        index = self.assets.index(chat_id)
        source_duration = float((project.transcript or {}).get("duration") or timeline.cut.source.end + 60)
        others = {c["number"]: c["version"] for c in self.db.current_clips(chat_id) if c["number"] != number}
        ctx = EditContext(assets=index, resolver=TimeResolver(base.duration, words), clock=base.clock,
                          source_duration=source_duration, number=number, clips=others,
                          source_words=tuple(getattr(project, "words", None) or ()))
        info = row["clip"]
        req = EditRequest(request, timeline, tuple(index.assets.values()), tuple(attached), tuple(words), number,
                          info.get("title", ""), info.get("alt_hook", ""), info.get("question", ""),
                          tuple(sorted(others)), info.get("hook", ""))
        plan = self.planner.plan(req, ctx)
        if plan.control("undo"):
            return self.undo(chat_id, number, plan.reply)
        if ask := plan.control("ask"):
            return EditOutcome(number, plan.reply, question=ask.question)
        result = plan.result
        if result is None or not result.ok:
            return EditOutcome(number, plan.reply, errors=plan.errors or ["no operations"])
        title_op = plan.control("set_title")
        changes = result.changes + ((Change.changed("title", title=title_op.title),) if title_op else ())
        if not changes:
            return EditOutcome(number, plan.reply)
        timeline = result.timeline.evolve(version=row["version"] + 1)
        clip = {**row["clip"], **({"title": title_op.title} if title_op else {})}
        out = self.storage.chat_clips(chat_id) / f"clip_{number:02d}_v{timeline.version}.mp4"
        captions = next(iter(timeline.layers_of("captions")), None)
        rendered = renderer.render(timeline, index, out, FINAL, captions.language if captions else None,
                                   self.joined(chat_id, timeline))
        hook = timeline.text_with_role("hook")
        clip["hook"] = rendered.info.get("hook") or (hook.text if hook else clip.get("hook", ""))
        txt = out.with_suffix(".txt")
        atomic_write_text(txt, post_text(Clip.from_dict(clip), project.source, rendered.info))
        version = NewVersion(clip, rendered.info, str(out), str(out.with_suffix(".jpg")), str(txt), timeline.to_json())
        return EditOutcome(number, plan.reply, changes, version=version)

    def joined(self, chat_id: int, timeline) -> dict[int, Path]:
        """The stored files of the clip versions this timeline plays after itself."""
        files = {}
        for join in timeline.joins:
            row = next((r for r in self.db.clip_versions(chat_id, join.clip) if r["version"] == join.version), None)
            if row and row.get("mp4") and Path(row["mp4"]).is_file():
                files[join.clip] = Path(row["mp4"])
        return files

    def undo(self, chat_id: int, number: int, reply: str = "") -> EditOutcome:
        """The previous version comes back as a new version (history is never rewritten)."""
        target = self.repo.undo_target(chat_id, number)
        if target is None:
            return EditOutcome(number, reply, notice="edit.nothing_to_undo")
        timeline = target.get("timeline") or self.repo.of(target).to_json()
        version = NewVersion(dict(target["clip"]), dict(target.get("info") or {}), target["mp4"], target["jpg"],
                             target["txt"], timeline)
        return EditOutcome(number, reply, (Change.changed("version", version=target["version"]),), version=version)
