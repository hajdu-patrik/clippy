"""Renders a timeline version: the base only when its key is new, then the finishing pass for the chosen profile."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from ...render import Encoder, fonts_dir_for, prepare_fonts
from ...concurrency import RENDER
from ...tools import log, move_atomic, report_span
from ..assets import AssetIndex
from ..timeline import Timeline
from .base import BaseRender, BaseRenderer, BaseStore
from .finishing import FINAL, FinishingRenderer, RenderProfile


@dataclass(frozen=True)
class TimelineRender:
    video: Path
    base: BaseRender
    info: dict


class TimelineRenderer:
    def __init__(self, project, store: BaseStore, finisher: FinishingRenderer | None = None):
        self.project, self.store = project, store
        encoder = getattr(getattr(project, "opts", None), "encoder", "auto")
        self.finisher = finisher or FinishingRenderer(Encoder.choose(encoder))

    def base(self, timeline: Timeline) -> tuple[BaseRender, bool]:
        key = timeline.base_key()
        cached = self.store.get(key)
        if cached is not None:
            log("[render] base: cached")
            return cached, True
        work = self.project.storage.scratch(self.project.scratch_root)
        try:
            return self.store.put(key, BaseRenderer(self.project).render(timeline, work)), False
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def render(self, timeline: Timeline, assets: AssetIndex, out_file: Path, profile: RenderProfile = FINAL,
               caption_language: str | None = None, joined: dict[int, Path] | None = None) -> TimelineRender:
        with report_span(0.0, 0.85):
            base, _ = self.base(timeline)
        work = self.project.storage.scratch(self.project.scratch_root)
        try:
            fonts = prepare_fonts(self.project.storage.cache("fonts"))
            fonts_dir = fonts_dir_for(work, fonts)
            translate = self.project.caption_translator(caption_language)
            draft = work / out_file.name
            with report_span(0.85, 1.0), RENDER.hold():
                info = self.finisher.render(timeline, base, assets, work, fonts_dir, draft, profile, translate,
                                            source=self.project.prepare().video if timeline.format == "16:9" else None,
                                            joined=joined)
            out_file.parent.mkdir(parents=True, exist_ok=True)
            if draft.with_suffix(".jpg").is_file():
                move_atomic(draft.with_suffix(".jpg"), out_file.with_suffix(".jpg"))
            move_atomic(draft, out_file)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return TimelineRender(out_file, base, {**base.info, **info})

