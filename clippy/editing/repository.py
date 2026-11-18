"""Timelines of stored clip versions. Versions are append-only: an undo is a new version with an older timeline."""

from __future__ import annotations

from .factory import timeline_from_clip
from .timeline import Timeline


class TimelineRepository:
    def __init__(self, db):
        self.db = db

    def of(self, row: dict, chat_options: dict | None = None) -> Timeline:
        """The timeline of a stored version; clips made before the editor get one built from their clip and options."""
        if row.get("timeline"):
            return Timeline.from_json(row["timeline"])
        options = {**(chat_options or {}), **(row.get("overrides") or {})}
        return timeline_from_clip(row["clip"], options, version=row["version"])

    def undo_target(self, chat_id: int, number: int) -> dict | None:
        """The version before the current state, or None when there is nothing to undo. A version made by an undo
        takes its edit off the history, so undoing again steps further back instead of redoing."""
        history = []
        for row in self.db.clip_versions(chat_id, number):
            if (row.get("info") or {}).get("restores") is not None and len(history) > 1:
                history.pop()
            else:
                history.append(row)
        return history[-2] if len(history) > 1 else None
