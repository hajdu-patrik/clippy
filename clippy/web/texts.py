"""Texts the server sends as {key, params}: the browser shows them in the UI language, the stored copy is English."""

from __future__ import annotations

from ..i18n import t

NEW_TITLE = "New chat"
PLACEHOLDER_TITLE = "Processing video…"


def tx(key: str, **params) -> dict:
    """A translatable text: the browser renders it in the UI language, the stored content is English."""
    return {"key": key, "params": {k: v for k, v in params.items() if v is not None}}


def english(tx: dict) -> str:
    return t(tx["key"], **tx["params"])
