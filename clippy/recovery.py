"""What to do when a job fails: give up on what cannot work, try again on what may work next time, and change the
settings that caused the failure (the GPU ran out of memory, the hardware encoder failed, no moment fit the length).
Each fix has steps; the next attempt takes the first step that is not in force yet."""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field

from .tools import ClippyError

MAX_ATTEMPTS = 3
PAUSES = (5.0, 20.0)

FINAL = frozenset({
    "error.chat_deleted", "error.cookies_file_missing", "error.cookies_unreadable", "error.download_gone", "error.download_live",
    "error.download_login", "error.download_playlist", "error.download_unsupported", "error.empty_transcript",
    "error.no_clip", "error.no_sound", "error.no_video", "error.not_public", "error.only_http", "error.unresolvable",
    "error.account_missing", "error.account_expired", "error.platform_not_configured", "error.platform_audit",
    "error.platform_rejected", "error.platform_slow", "error.clip_too_long_for", "error.no_platform",
    "error.unknown_platform", "error.bad_token", "error.platform_rate_limited", "error.posting_unsupported",
    "error.no_page", "error.ffmpeg_missing", "error.ytdlp_missing", "error.no_file", "error.no_video_stream",
    "error.ollama_no_model", "error.llm_none", "error.llm_backend", "error.llm_base_url", "error.isolation_extras",
    "error.ducking_extras", "error.claude_missing", "error.claude_login", "error.codex_missing", "error.codex_login",
    "error.llm_openrouter_key", "error.llm_auth", "error.llm_cli_limit",
})


@dataclass(frozen=True)
class Fix:
    reason: str
    options: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Rule:
    reason: str
    words: tuple[str, ...] = ()
    keys: tuple[str, ...] = ()
    steps: tuple[dict, ...] = ({},)


RULES = (
    Rule("gpu_memory", ("out of memory", "outofmemoryerror", "cuda error", "cublas", "cudnn"),
         steps=({"isolate": "off"}, {"device": "cpu"})),
    Rule("encoder", ("nvenc", "openencodesession", "no capable devices", "nvcuda"), steps=({"encoder": "x264"},)),
    Rule("voice", ("demucs", "separation.py"), steps=({"isolate": "off"},)),
    Rule("alignment", ("align.py", "wav2vec", "mms_fa"), steps=({"align": False},)),
    Rule("length", keys=("error.no_suitable_clip",), steps=({"min_len": 10.0, "max_len": 90.0},)),
    Rule("llm", ("ollama", "llm"), keys=("error.llm_unreachable", "error.ollama_unreachable", "error.llm_failed",
                                         "error.llm_api", "error.llm_json", "error.llm_cli_timeout")),
    Rule("network", ("403", "429", "timed out", "timeout", "connection", "temporarily", "network", "unable to download",
                     "http error"), keys=("error.download_failed", "error.download_busy", "error.download_missing",
                           "error.platform_network")),
)


def describe(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc)).lower()


def diagnose(exc: BaseException, in_force: dict | None = None) -> Fix | None:
    """None: trying again cannot help. Otherwise why it failed and what the next attempt changes."""
    key = exc.key if isinstance(exc, ClippyError) else None
    if key in FINAL:
        return None
    text, in_force = describe(exc), in_force or {}
    rule = next((r for r in RULES if key in r.keys or any(w in text for w in r.words)), None)
    if rule is None:
        return Fix("unknown")
    step = next((s for s in rule.steps if any(in_force.get(k) != v for k, v in s.items())), None)
    if step is None and rule.reason == "length":
        return None
    return Fix(rule.reason, dict(step or {}))


def pause(attempt: int) -> float:
    """Seconds to wait before attempt `attempt` (2, 3…): a network hiccup or a model that is still loading."""
    return PAUSES[min(attempt - 2, len(PAUSES) - 1)]
