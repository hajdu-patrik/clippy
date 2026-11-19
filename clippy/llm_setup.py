"""Choosing the AI: what Settings and `clippy llm` read and write. The choice (and an OpenRouter key) is kept in
data/web/llm.json, which git ignores, and a key is only ever shown masked."""

from __future__ import annotations

import argparse
import getpass
import re
import sys
import threading
import time
from collections.abc import Callable

from . import i18n
from .llm import (DEFAULT_MODELS, MODEL_NAME, MODES, CliLLM, build_llm, key_of, mode_of, model_of, stored,
                  unload_all)
from .shell import command_doc, flags_of
from .storage import Storage
from .tools import ClippyError, write_json

KEY_FORMAT = re.compile(r"[\x21-\x7e]{8,300}")
NAMED = ("ollama", "openrouter", "claude", "codex")
KEYED = ("openrouter",)
PROBE_TIMEOUT = 120
PROBE_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def mask(key: str) -> str:
    return "" if not key else f"…{key[-4:]}" if len(key) >= 12 else "…"


def public(storage: Storage) -> dict:
    """The choice for the browser: the mode in force, whether the user ever chose, and each mode's model and masked key."""
    config = stored(storage)
    mode = mode_of(config)
    ids = (*MODES, "openai") if mode == "openai" else MODES
    return {"mode": mode, "chosen": config.get("mode") in MODES,
            "modes": [{"id": m, "model": model_of(m, config) if m in NAMED else "",
                       "default": DEFAULT_MODELS.get(m, ""), "key": mask(key_of(m, config)) if m in KEYED else ""}
                      for m in ids]}


def save(storage: Storage, mode: str, model: str = "", key: str = "") -> dict:
    """Make `mode` the AI. An empty model means the default, an empty key keeps the one saved."""
    if mode not in MODES:
        raise ClippyError(key="error.llm_backend", name=mode, choices=", ".join(MODES))
    model, key = model.strip(), key.strip()
    if not MODEL_NAME.fullmatch(model):
        raise ClippyError(key="error.llm_model_name")
    if key and not KEY_FORMAT.fullmatch(key):
        raise ClippyError(key="error.llm_key_format")
    config = stored(storage)
    models, keys = dict(config.get("models") or {}), dict(config.get("keys") or {})
    if mode in NAMED:
        models.pop(mode, None)
        if model:
            models[mode] = model
    if key and mode in KEYED:
        keys[mode] = key
    if mode == "openrouter" and not key_of(mode, {"keys": keys}):
        raise ClippyError(key="error.llm_openrouter_key")
    if mode_of(config) == "ollama" and mode != "ollama":
        unload_all()
    write_json(storage.llm_file, {"mode": mode, "models": models, "keys": keys})
    PROBLEMS.clear()
    return public(storage)


def candidate(storage: Storage, mode: str, model: str = "", key: str = ""):
    """The backend for a mode as typed in Settings (nothing saved yet): blanks fall back to what is saved."""
    config = stored(storage)
    name = mode_of(config, mode)
    key = key.strip()
    if key and not KEY_FORMAT.fullmatch(key):
        raise ClippyError(key="error.llm_key_format")
    made = build_llm(name, model.strip() or model_of(name, config), key or key_of(name, config))
    if isinstance(made, CliLLM):
        made.timeout = PROBE_TIMEOUT
    return made


def probe(llm) -> dict:
    """One tiny round trip, to see that the whole way to the model works."""
    problem = llm.problem()
    if problem is not None:
        raise problem
    started = time.monotonic()
    llm.chat_json("You are a health check. Answer with JSON only.", 'Return {"ok": true}.', schema=PROBE_SCHEMA)
    return {"ok": True, "seconds": round(time.monotonic() - started, 1), "llm": llm.describe()}


class ProblemCache:
    """What a mode's readiness check said, kept for a few seconds, with at most one check of a kind running."""

    def __init__(self, ttl: float = 10.0, clock: Callable[[], float] = time.monotonic):
        self.ttl, self.clock = ttl, clock
        self._guard = threading.Lock()
        self._slots: dict[tuple, list] = {}

    def clear(self) -> None:
        with self._guard:
            self._slots.clear()

    def get(self, key: tuple, check: Callable[[], ClippyError | None]) -> ClippyError | None:
        with self._guard:
            slot = self._slots.setdefault(key, [threading.Lock(), None, None])
        with slot[0]:
            if slot[2] is None or self.clock() - slot[2] > self.ttl:
                slot[1], slot[2] = check(), self.clock()
            return slot[1]


PROBLEMS = ProblemCache()


def problem(storage: Storage, mode: str, model: str = "", key: str = "") -> ClippyError | None:
    """Why a mode cannot answer right now, as typed in Settings; the answer is reused for a few seconds."""
    made = candidate(storage, mode, model, key)
    return PROBLEMS.get((mode, model, bool(key.strip())), made.problem)


def tr(key: str, **params) -> str:
    return i18n.t(key, **params)


def _model_text(model: str) -> str:
    return f" ({model})" if model else ""


def _apply(storage: Storage, mode: str, model: str | None, secret: Callable[[str], str]) -> None:
    """Save the choice; OpenRouter without a key asks for it (hidden) when there is a person to ask."""
    config = stored(storage)
    key = ""
    if mode == "openrouter" and not key_of(mode, config) and sys.stdin.isatty():
        key = secret(tr("cli.llm_key_prompt"))
    save(storage, mode, model if model is not None else (config.get("models") or {}).get(mode, ""), key)


def first_run(say: Callable[[str], None], storage: Storage | None = None, ask: Callable[[str], str] = input,
              secret: Callable[[str], str] = getpass.getpass) -> bool:
    """Ask once, when no AI was ever chosen and someone is there to answer; True when a choice was saved."""
    storage = storage or Storage()
    if stored(storage).get("mode") in MODES or not sys.stdin.isatty():
        return False
    say(tr("cli.llm_ask"))
    for number, mode in enumerate(MODES, 1):
        print(f"  {number}  {mode:<11}{tr('cli.llm_about_' + mode)}")
    while True:
        try:
            answer = ask(tr("cli.llm_pick")).strip().lower() or "1"
        except (EOFError, KeyboardInterrupt):
            return False
        mode = MODES[int(answer) - 1] if answer.isdigit() and 0 < int(answer) <= len(MODES) else answer
        if mode in MODES:
            break
    try:
        _apply(storage, mode, None, secret)
    except ClippyError as exc:
        say(str(exc))
        return False
    say(tr("cli.llm_saved", mode=mode, model=_model_text(model_of(mode, stored(storage)) if mode in NAMED else "")))
    return True


def listing(storage: Storage, say: Callable[[str], None]) -> None:
    """Every mode with a mark on the one in force and whether it is ready to answer."""
    config = stored(storage)
    current = mode_of(config)
    for mode in MODES:
        problem = build_llm(mode, model_of(mode, config), key_of(mode, config)).problem()
        say(tr("cli.llm_line", mark="*" if mode == current else " ", mode=mode,
               state=tr("cli.llm_ready") if problem is None else str(problem)))


def command(args: list[str], say: Callable[[str], None], storage: Storage | None = None) -> int:
    """`clippy llm [mode] [--model NAME] [--test]`: show the AI, change it, check it."""
    parser = argparse.ArgumentParser(prog="clippy llm", description=command_doc("llm"))
    parser.add_argument("mode", nargs="?", choices=MODES, help=", ".join(MODES))
    for flag, doc, _ in flags_of("llm"):
        parser.add_argument(flag, help=doc, **({"action": "store_true"} if flag == "--test" else {"default": None}))
    given = parser.parse_args(args)
    storage = storage or Storage()
    try:
        config = stored(storage)
        mode = given.mode or (mode_of(config) if given.model is not None else None)
        if mode is None and not given.test:
            listing(storage, say)
            return 0
        if mode is not None:
            _apply(storage, mode, given.model, getpass.getpass)
            say(tr("cli.llm_saved", mode=mode, model=_model_text(model_of(mode, stored(storage)) if mode in NAMED else "")))
        if given.test:
            done = probe(candidate(storage, mode_of(stored(storage))))
            say(tr("cli.llm_tested", seconds=done["seconds"], llm=done["llm"]))
    except ClippyError as exc:
        say(str(exc))
        return 1
    return 0
