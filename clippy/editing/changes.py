"""What an edit changed, as data: the chat shows it in the reader's language, tests compare it exactly."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .. import i18n
from .timeline import Frozen


class Change(Frozen):
    action: Literal["added", "changed", "removed"]
    subject: str = Field(pattern=r"^[a-z_]+$")
    params: dict[str, str | int | float] = {}

    @classmethod
    def added(cls, subject: str, **params) -> "Change":
        return cls(action="added", subject=subject, params=params)

    @classmethod
    def changed(cls, subject: str, **params) -> "Change":
        return cls(action="changed", subject=subject, params=params)

    @classmethod
    def removed(cls, subject: str, **params) -> "Change":
        return cls(action="removed", subject=subject, params=params)

    def text(self, lang: str) -> str:
        what = i18n.t(lang, f"what.{self.subject}", **self.params)
        return i18n.t(lang, f"change.{self.action}", what=what)
