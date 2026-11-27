import pytest

from clippy import recovery
from clippy.tools import ClippyError


def fail(text: str) -> Exception:
    """An error with only its text: a raised one would carry this file's path, which names the checkout's folder."""
    return RuntimeError(text)


def test_hopeless_failures_are_not_tried_again():
    assert recovery.diagnose(ClippyError(key="error.not_public", host="x")) is None
    assert recovery.diagnose(ClippyError(key="error.download_login", host="x", detail="")) is None
    assert recovery.diagnose(ClippyError(key="error.ollama_no_model", model="m")) is None
    assert recovery.diagnose(ClippyError(key="error.isolation_extras")) is None


def test_every_key_the_rules_name_is_in_the_catalogue():
    from clippy.i18n import CATALOG

    named = set(recovery.FINAL) | {key for rule in recovery.RULES for key in rule.keys}
    assert named <= set(CATALOG)


@pytest.mark.parametrize("text, reason, options", [
    ("CUDA error: out of memory", "gpu_memory", {"isolate": "off"}),
    ("[h264_nvenc @ 0x1] OpenEncodeSessionEx failed: no capable devices", "encoder", {"encoder": "x264"}),
    ("HTTP Error 403: Forbidden", "network", {}),
    ("something odd", "unknown", {}),
])
def test_each_failure_gets_its_fix(text, reason, options):
    assert recovery.diagnose(fail(text)) == recovery.Fix(reason, options)


def test_fixes_go_one_step_further_each_time():
    oom = fail("torch.OutOfMemoryError: CUDA out of memory")
    assert recovery.diagnose(oom, {"isolate": "off"}).options == {"device": "cpu"}
    assert recovery.diagnose(oom, {"isolate": "off", "device": "cpu"}).options == {}
    tight = ClippyError(key="error.no_suitable_clip")
    assert recovery.diagnose(tight).options == {"min_len": 10.0, "max_len": 90.0}
    assert recovery.diagnose(tight, {"min_len": 10.0, "max_len": 90.0}) is None
    assert recovery.diagnose(ClippyError(key="error.llm_unreachable", error="x")).reason == "llm"
    assert recovery.pause(2) < recovery.pause(3) == recovery.pause(9)
