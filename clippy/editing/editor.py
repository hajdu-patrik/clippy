"""Runs a list of operations against a timeline. Validation is a dry run of the same code: every operation is applied
in order, and what fails is collected as an issue with a readable message (shown to the user, fed back to the LLM)."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import ValidationError

from .. import i18n
from ..tools import ClippyError
from .changes import Change
from .operations import EditContext, Operation
from .timeline import Timeline


@dataclass(frozen=True)
class Issue:
    index: int
    kind: str
    message: str
    key: str | None = None
    params: dict = field(default_factory=dict)

    def text(self, lang: str) -> str:
        return i18n.t(lang, self.key, **self.params) if self.key else self.message

    def for_model(self) -> str:
        return f"#{self.index} {self.kind}: {self.message}"


@dataclass(frozen=True)
class EditResult:
    timeline: Timeline
    changes: tuple[Change, ...]
    issues: tuple[Issue, ...]
    controls: tuple[Operation, ...]
    touches_base: bool

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def changed(self) -> bool:
        return bool(self.changes)


class Editor:
    def run(self, timeline: Timeline, ops: list[Operation], ctx: EditContext) -> EditResult:
        current, changes, issues, controls, base = timeline, [], [], [], False
        for i, op in enumerate(ops):
            if op.control:
                controls.append(op)
                continue
            try:
                current, new = op.apply(current, ctx)
            except ClippyError as exc:
                issues.append(Issue(i, op.kind, str(exc), exc.key, dict(exc.params)))
                continue
            except ValidationError as exc:
                message = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
                issues.append(Issue(i, op.kind, message))
                continue
            changes.extend(new)
            base = base or (op.touches_base and bool(new))
        if changes and not issues:
            current = current.next_version()
        return EditResult(current if not issues else timeline, tuple(changes), tuple(issues), tuple(controls),
                          base and not issues)
