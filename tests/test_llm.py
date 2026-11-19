import json
import subprocess

import pytest

from clippy import llm, llm_setup
from clippy.concurrency import GuardedLLM
from clippy.storage import Storage
from clippy.tools import ClippyError


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    for name in ("BACKEND", "MODEL", "BASE_URL", "API_KEY", "CLI_TIMEOUT"):
        monkeypatch.delenv(f"CLIPPY_LLM_{name}", raising=False)
    monkeypatch.setattr(llm_setup, "unload_all", lambda: [])
    monkeypatch.setattr(llm.requests, "get", lambda *a, **k: (_ for _ in ()).throw(llm.requests.ConnectionError()))
    return Storage(tmp_path / "data", tmp_path / "library")


class FakeProc:
    def __init__(self, out="", err="", code=0, hang=False, write=None):
        self.out, self.err, self.returncode, self.hang, self.write = out, err, code, hang, write
        self.pid, self.fed = 4242, None

    def communicate(self, data=None, timeout=None):
        if data is not None:
            self.fed = data.decode("utf-8")
        if self.hang and timeout:
            self.hang = False
            raise subprocess.TimeoutExpired("tool", timeout)
        if self.write:
            self.write()
        return self.out.encode("utf-8"), self.err.encode("utf-8")


@pytest.fixture
def tools(monkeypatch):
    """Both command line tools "installed"; each launch is recorded and answered by the next queued FakeProc."""
    launched, queue = [], []
    monkeypatch.setattr(llm.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(llm, "_kill_tree", lambda proc: launched.append(("killed", proc.pid)))

    def popen(argv, **kwargs):
        launched.append((argv, kwargs))
        return queue.pop(0)

    monkeypatch.setattr(llm.subprocess, "Popen", popen)
    return launched, queue


def test_every_mode_has_a_backend_and_the_default_is_local_ollama(clean):
    assert set(llm.MODES) <= set(llm.BACKENDS) and "openai" in llm.BACKENDS
    made = llm.create_llm(storage=clean)
    assert made.backend == "ollama" and made.model == llm.DEFAULT_MODEL and made.base_url == llm.OLLAMA_URL


def test_the_saved_choice_picks_the_backend_and_its_own_model(clean):
    llm_setup.save(clean, "openrouter", "meta-llama/llama-3.3-70b-instruct:free", "sk-or-v1-abcdef123456")
    made = llm.create_llm(storage=clean)
    assert made.backend == "openrouter" and made.base_url == llm.OPENROUTER_URL
    assert made.model == "meta-llama/llama-3.3-70b-instruct:free" and made.api_key == "sk-or-v1-abcdef123456"
    assert llm.create_llm("none", storage=clean).backend == "none"
    assert llm.create_llm("ollama", storage=clean).model == llm.DEFAULT_MODEL
    assert llm.create_llm(None, "x:free", clean).model == "x:free"
    llm_setup.save(clean, "claude", "sonnet")
    assert llm.create_llm(storage=clean).backend == "claude" and llm.create_llm(storage=clean).model == "sonnet"
    llm_setup.save(clean, "codex")
    assert llm.create_llm(storage=clean).backend == "codex" and llm.create_llm(storage=clean).model == ""


def test_without_a_saved_choice_the_environment_still_decides(clean, monkeypatch):
    monkeypatch.setenv("CLIPPY_LLM_BACKEND", "openai")
    monkeypatch.setenv("CLIPPY_LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("CLIPPY_LLM_MODEL", "big-model")
    monkeypatch.setenv("CLIPPY_LLM_API_KEY", "env-key-123456")
    made = llm.create_llm(storage=clean)
    assert (made.backend, made.model, made.api_key) == ("openai", "big-model", "env-key-123456")
    assert llm_setup.public(clean)["mode"] == "openai" and not llm_setup.public(clean)["chosen"]
    assert llm.create_llm("claude", storage=clean).model == ""
    monkeypatch.delenv("CLIPPY_LLM_BASE_URL")
    with pytest.raises(ClippyError) as err:
        llm.create_llm(storage=clean)
    assert err.value.key == "error.llm_base_url"
    with pytest.raises(ClippyError) as unknown:
        llm.create_llm("gemini", storage=clean)
    assert unknown.value.key == "error.llm_backend"


def test_openrouter_sends_the_key_and_the_schema_and_names_a_refused_key(monkeypatch):
    seen = {}

    class Reply:
        status_code, ok, text = 200, True, ""

        def json(self):
            return {"choices": [{"message": {"content": '{"a": 1}'}}]}

    def post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, payload=json, headers=headers)
        return Reply()

    monkeypatch.setattr(llm.requests, "post", post)
    made = llm.build_llm("openrouter", "m:free", "sk-or-key-123456")
    assert made.chat_json("system", "question", schema={"type": "object"}) == {"a": 1}
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer sk-or-key-123456" and seen["headers"]["X-Title"] == "Clippy"
    assert '"type": "object"' in seen["payload"]["messages"][1]["content"] and seen["payload"]["model"] == "m:free"

    Reply.status_code, Reply.ok = 401, False
    with pytest.raises(ClippyError) as refused:
        made.chat_json("s", "u")
    assert refused.value.key == "error.llm_auth"
    with pytest.raises(ClippyError) as no_key:
        llm.build_llm("openrouter", "m:free").chat_json("s", "u")
    assert no_key.value.key == "error.llm_openrouter_key"
    assert llm.build_llm("openrouter", "m").problem().key == "error.llm_openrouter_key"
    assert llm.build_llm("openrouter", "m", "key-123456789").problem() is None


def test_claude_is_called_without_tools_a_shell_or_a_window(tools):
    launched, queue = tools
    queue.append(FakeProc(out=json.dumps({"type": "result", "is_error": False, "result": '```json\n{"hook": "Wow"}\n```'})))
    made = llm.build_llm("claude", "haiku")
    assert made.chat_json("be brief", "pick a hook", schema={"type": "object"}) == {"hook": "Wow"}
    argv, kwargs = launched[0]
    assert argv[:4] == ["/bin/claude", "-p", "--output-format", "json"]
    assert ["--tools", ""] == argv[argv.index("--tools"):argv.index("--tools") + 2]
    assert "--no-session-persistence" in argv and argv[argv.index("--model") + 1] == "haiku"
    assert "shell" not in kwargs and kwargs["env"].get("ANTHROPIC_API_KEY") is None


def test_claude_gets_the_system_prompt_from_a_file_and_the_question_on_stdin(tools, monkeypatch):
    launched, queue = tools
    proc = FakeProc(out=json.dumps({"is_error": False, "result": "{}"}))
    queue.append(proc)
    seen = {}
    real = llm.Path.read_text

    def spy(self, *args, **kwargs):
        text = real(self, *args, **kwargs)
        seen[self.name] = text
        return text

    def popen_reading(argv, **kwargs):
        launched.append((argv, kwargs))
        seen["system"] = real(llm.Path(argv[argv.index("--system-prompt-file") + 1]), encoding="utf-8")
        return queue.pop(0)

    monkeypatch.setattr(llm.subprocess, "Popen", popen_reading)
    llm.build_llm("claude", "").chat_json("SYSTEM TEXT", "USER TEXT")
    assert seen["system"] == "SYSTEM TEXT" and "USER TEXT" in proc.fed and "JSON" in proc.fed
    assert "--model" not in launched[0][0]


def test_a_missing_tool_a_login_a_limit_a_timeout_and_bad_output_are_told_apart(tools, monkeypatch):
    launched, queue = tools
    made = llm.build_llm("claude", "")

    queue.append(FakeProc(out=json.dumps({"is_error": True, "result": "Not logged in · Please run /login"}), code=1))
    with pytest.raises(ClippyError) as login:
        made.chat_json("s", "u")
    assert login.value.key == "error.claude_login" and "claude" in str(login.value)

    queue.append(FakeProc(out=json.dumps({"is_error": True, "result": "You've hit your usage limit"}), code=1))
    with pytest.raises(ClippyError) as limit:
        made.chat_json("s", "u")
    assert limit.value.key == "error.llm_cli_limit"

    queue.append(FakeProc(hang=True))
    made.timeout = 5
    with pytest.raises(ClippyError) as slow:
        made.chat_json("s", "u")
    assert slow.value.key == "error.llm_cli_timeout" and slow.value.params == {"tool": "claude", "seconds": 5}
    assert ("killed", 4242) in launched

    queue.append(FakeProc(out=json.dumps({"is_error": False, "result": "no json here"})))
    with pytest.raises(ClippyError) as bad:
        made.chat_json("s", "u")
    assert bad.value.key == "error.llm_json"

    queue.append(FakeProc(out="Segmentation fault", code=139))
    with pytest.raises(ClippyError) as crashed:
        made.chat_json("s", "u")
    assert crashed.value.key == "error.llm_failed"

    monkeypatch.setattr(llm.shutil, "which", lambda name: None)
    with pytest.raises(ClippyError) as missing:
        made.chat_json("s", "u")
    assert missing.value.key == "error.claude_missing" and "npm install" in str(missing.value)
    assert made.problem().key == "error.claude_missing"

    monkeypatch.setattr(llm.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(llm.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(ClippyError) as vanished:
        made.chat_json("s", "u")
    assert vanished.value.key == "error.claude_missing"


def test_the_sign_in_is_read_from_the_tools_own_status(tools):
    _, queue = tools
    queue += [FakeProc(out=json.dumps({"loggedIn": True})), FakeProc(out=json.dumps({"loggedIn": False}), code=1),
              FakeProc(out="Logged in using ChatGPT"), FakeProc(out="Not logged in", code=1)]
    assert llm.build_llm("claude", "").problem() is None
    assert llm.build_llm("claude", "").problem().key == "error.claude_login"
    assert llm.build_llm("codex", "").problem() is None
    assert llm.build_llm("codex", "").problem().key == "error.codex_login"


def test_codex_runs_read_only_and_its_last_message_is_the_answer(tools):
    launched, queue = tools

    def write_answer():
        argv = launched[-1][0]
        llm.Path(argv[argv.index("--output-last-message") + 1]).write_text('{"clips": [1, 2]}\n', encoding="utf-8")

    queue += [FakeProc(out="Logged in using ChatGPT"), FakeProc(out="progress noise", write=write_answer)]
    proc = queue[1]
    made = llm.build_llm("codex", "gpt-5")
    assert made.chat_json("SYSTEM TEXT", "USER TEXT", schema={"type": "object"}) == {"clips": [1, 2]}
    argv, kwargs = launched[-1]
    assert argv[:2] == ["/bin/codex", "exec"] and argv[-1] == "-"
    assert argv[argv.index("--sandbox") + 1] == "read-only" and "--skip-git-repo-check" in argv
    assert argv[argv.index("--model") + 1] == "gpt-5" and "shell" not in kwargs
    assert "SYSTEM TEXT" in proc.fed and "USER TEXT" in proc.fed and '"type": "object"' in proc.fed


def test_codex_failures_name_what_to_do(tools):
    launched, queue = tools
    made = llm.build_llm("codex", "")
    queue.append(FakeProc(out="Not logged in", code=1))
    with pytest.raises(ClippyError) as login:
        made.chat_json("s", "u")
    assert login.value.key == "error.codex_login" and "codex login" in str(login.value) and len(launched) == 1

    queue += [FakeProc(out="Logged in"), FakeProc(err="warning: x\nERROR: You've hit your usage limit. Try later.\n", code=1)]
    with pytest.raises(ClippyError) as limit:
        made.chat_json("s", "u")
    assert limit.value.key == "error.llm_cli_limit"

    queue += [FakeProc(out="Logged in"), FakeProc(hang=True)]
    made.timeout = 3
    with pytest.raises(ClippyError) as slow:
        made.chat_json("s", "u")
    assert slow.value.key == "error.llm_cli_timeout"

    queue += [FakeProc(out="Logged in"), FakeProc(out="", code=0)]
    with pytest.raises(ClippyError) as empty:
        made.chat_json("s", "u")
    assert empty.value.key == "error.llm_failed"


def test_the_choice_is_saved_locally_and_a_key_is_only_ever_shown_masked(clean):
    assert llm_setup.public(clean)["chosen"] is False and llm_setup.public(clean)["mode"] == "ollama"
    saved = llm_setup.save(clean, "openrouter", "qwen/qwen3-235b-a22b:free", "sk-or-v1-0123456789abcdef")
    assert saved["mode"] == "openrouter" and saved["chosen"] is True
    row = next(m for m in saved["modes"] if m["id"] == "openrouter")
    assert row["key"] == "…cdef" and row["model"] == "qwen/qwen3-235b-a22b:free"
    assert "0123456789" not in json.dumps(saved)
    assert json.loads(clean.llm_file.read_text(encoding="utf-8"))["keys"] == {"openrouter": "sk-or-v1-0123456789abcdef"}

    again = llm_setup.save(clean, "openrouter", "other:free")
    assert next(m for m in again["modes"] if m["id"] == "openrouter")["key"] == "…cdef"
    assert llm.create_llm(storage=clean).api_key == "sk-or-v1-0123456789abcdef"

    llm_setup.save(clean, "claude", "opus")
    back = llm_setup.public(clean)
    assert back["mode"] == "claude" and [m["id"] for m in back["modes"]] == list(llm.MODES)
    assert next(m for m in back["modes"] if m["id"] == "claude")["model"] == "opus"
    assert next(m for m in back["modes"] if m["id"] == "openrouter")["model"] == "other:free"
    assert next(m for m in back["modes"] if m["id"] == "ollama")["model"] == llm.DEFAULT_MODEL

    llm_setup.save(clean, "claude", "")
    assert next(m for m in llm_setup.public(clean)["modes"] if m["id"] == "claude")["model"] == ""


def test_a_wrong_choice_is_refused_and_changes_nothing(clean):
    for args, key in ((("gemini",), "error.llm_backend"), (("claude", "bad name; rm -rf"), "error.llm_model_name"),
                      (("openrouter", "m:free", "short"), "error.llm_key_format"),
                      (("openrouter", "m:free"), "error.llm_openrouter_key")):
        with pytest.raises(ClippyError) as err:
            llm_setup.save(clean, *args)
        assert err.value.key == key
    assert not clean.llm_file.exists()


def test_leaving_ollama_frees_its_memory(clean, monkeypatch):
    freed = []
    monkeypatch.setattr(llm_setup, "unload_all", lambda: freed.append(1) or [])
    llm_setup.save(clean, "claude")
    llm_setup.save(clean, "codex")
    llm_setup.save(clean, "ollama")
    llm_setup.save(clean, "ollama")
    assert freed == [1]


def test_the_probe_does_one_tiny_round_trip_and_stops_at_a_known_problem(clean, tools):
    launched, queue = tools
    queue += [FakeProc(out=json.dumps({"loggedIn": True})),
              FakeProc(out=json.dumps({"is_error": False, "result": '{"ok": true}'}))]
    done = llm_setup.probe(llm_setup.candidate(clean, "claude", "haiku"))
    assert done["ok"] is True and done["llm"] == "claude:haiku" and len(launched) == 2

    with pytest.raises(ClippyError) as key:
        llm_setup.probe(llm_setup.candidate(clean, "openrouter", "m:free"))
    assert key.value.key == "error.llm_openrouter_key"
    typed = llm_setup.candidate(clean, "openrouter", "m:free", "sk-or-typed-123456")
    assert typed.api_key == "sk-or-typed-123456" and not clean.llm_file.exists()
    assert GuardedLLM(typed).problem() is None


def test_the_command_shows_changes_and_checks_the_ai(clean, tools):
    launched, queue = tools
    said = []
    queue += [FakeProc(out=json.dumps({"loggedIn": True})), FakeProc(out=json.dumps({"loggedIn": True}))]
    assert llm_setup.command(["claude", "--model", "haiku"], said.append, clean) == 0
    assert said == ["AI: claude (haiku). It applies from the next request; no restart needed."]
    assert llm.create_llm(storage=clean).model == "haiku"

    queue.clear()
    queue += [FakeProc(out=json.dumps({"loggedIn": True})),
              FakeProc(out=json.dumps({"is_error": False, "result": '{"ok": true}'}))]
    said.clear()
    assert llm_setup.command(["--test"], said.append, clean) == 0 and said[0].startswith("The AI answered")

    said.clear()
    assert llm_setup.command(["codex"], said.append, clean) == 0 and llm.create_llm(storage=clean).backend == "codex"
    assert llm_setup.command(["openrouter"], said.append, clean) == 1 and "OpenRouter needs an API key" in said[-1]

    queue.clear()
    queue += [FakeProc(out="Logged in"), FakeProc(out=json.dumps({"loggedIn": False}), code=1)]
    said.clear()
    assert llm_setup.command([], said.append, clean) == 0 and len(said) == len(llm.MODES)
    assert said[0].startswith("  ollama") and "not reachable" in said[0] and "needs an API key" in said[1]
    assert said[2].startswith("  claude") and said[2].endswith("ready")
    assert said[3].startswith("* codex") and "not signed in" in said[3] and said[4].endswith("ready")


def test_the_first_question_is_asked_once_and_only_to_a_person(clean, monkeypatch):
    said = []
    monkeypatch.setattr(llm_setup.sys.stdin, "isatty", lambda: False)
    assert llm_setup.first_run(said.append, clean, ask=lambda q: pytest.fail("asked")) is False and not said
    monkeypatch.setattr(llm_setup.sys.stdin, "isatty", lambda: True)
    assert llm_setup.first_run(said.append, clean, ask=lambda q: "3", secret=lambda q: "") is True
    assert llm.create_llm(storage=clean).backend == "claude"
    assert llm_setup.first_run(said.append, clean, ask=lambda q: pytest.fail("asked twice")) is False

    fresh = Storage(clean.data / "other", clean.library)
    answers = iter(["nonsense", "2"])
    assert llm_setup.first_run(said.append, fresh, ask=lambda q: next(answers), secret=lambda q: "sk-or-typed-123456")
    assert llm.create_llm(storage=fresh).api_key == "sk-or-typed-123456"
    assert llm_setup.first_run(said.append, Storage(clean.data / "third", clean.library), ask=lambda q: "") is True
    assert llm.create_llm(storage=Storage(clean.data / "third", clean.library)).backend == "ollama"


def test_every_command_line_text_exists():
    from clippy import i18n

    for mode in llm.MODES:
        assert f"cli.llm_about_{mode}" in i18n.CATALOG
    for backend in ("claude", "codex"):
        assert f"error.{backend}_missing" in i18n.CATALOG and f"error.{backend}_login" in i18n.CATALOG
