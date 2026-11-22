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

    queue += [FakeProc(out="Logged in using ChatGPT"), FakeProc(err="warning: x\nERROR: You've hit your usage limit. Try later.\n", code=1)]
    with pytest.raises(ClippyError) as limit:
        made.chat_json("s", "u")
    assert limit.value.key == "error.llm_cli_limit"

    queue += [FakeProc(out="Logged in using ChatGPT"), FakeProc(hang=True)]
    made.timeout = 3
    with pytest.raises(ClippyError) as slow:
        made.chat_json("s", "u")
    assert slow.value.key == "error.llm_cli_timeout"

    queue += [FakeProc(out="Logged in using ChatGPT"), FakeProc(out="", code=0)]
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


def test_a_command_line_tool_gets_only_the_system_basics_and_never_a_key(tools, monkeypatch):
    launched, queue = tools
    for name, value in (("ANTHROPIC_API_KEY", "a"), ("ANTHROPIC_BASE_URL", "b"), ("CLAUDE_CODE_X", "c"),
                        ("CLIPPY_LLM_API_KEY", "d"), ("OPENAI_API_KEY", "e"), ("CODEX_API_KEY", "f"),
                        ("SECRET_TOKEN", "g"), ("PATH", "/bin"), ("https_proxy", "http://p:1"), ("LC_ALL", "C"),
                        ("ProgramFiles(x86)", "/pf"), ("CODEX_HOME", "/ch"), ("CLAUDE_CONFIG_DIR", "/cc")):
        monkeypatch.setenv(name, value)
    queue.append(FakeProc(out=json.dumps({"is_error": False, "result": "{}"})))
    llm.build_llm("claude", "").chat_json("s", "u")
    env = launched[0][1]["env"]
    names = {k.upper() for k in env}
    assert {"PATH", "HTTPS_PROXY", "LC_ALL", "PROGRAMFILES(X86)", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "NO_COLOR"} <= names
    assert not {n for n in names if n.startswith(("ANTHROPIC", "CLIPPY", "OPENAI", "SECRET")) or n == "CODEX_API_KEY"}
    assert "CLAUDE_CODE_X" not in env


def test_codex_has_no_tools_beyond_the_question(tools):
    launched, queue = tools
    holder = {}

    def write_answer():
        argv = launched[-1][0]
        holder["schema"] = "--output-schema" in argv
        llm.Path(argv[argv.index("--output-last-message") + 1]).write_text("{}", encoding="utf-8")

    strict = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"], "additionalProperties": False}
    for schema, used in ((strict, True), ({"type": "object", "properties": {"a": {}}}, False), (None, False)):
        queue += [FakeProc(out="Logged in using ChatGPT"), FakeProc(write=write_answer)]
        llm.build_llm("codex", "").chat_json("s", "u", schema=schema)
        argv = launched[-1][0]
        assert holder["schema"] is used
        disabled = [argv[i + 1] for i, a in enumerate(argv) if a == "--disable"]
        assert {"apps", "plugins", "remote_plugin", "browser_use", "computer_use", "shell_tool"} <= set(disabled)
        assert argv[argv.index("-c") + 1] == "web_search=disabled"
    assert llm.strict_schema(strict) and not llm.strict_schema({"type": "array", "items": {"type": "object"}})


def test_codex_signed_in_with_an_api_key_is_not_the_subscription(tools):
    launched, queue = tools
    queue.append(FakeProc(out="Logged in using an API key - sk-***abcd"))
    made = llm.build_llm("codex", "")
    assert made.problem().key == "error.codex_not_subscription"
    queue.append(FakeProc(out="Logged in using an API key - sk-***abcd"))
    with pytest.raises(ClippyError) as err:
        made.chat_json("s", "u")
    assert err.value.key == "error.codex_not_subscription" and len(launched) == 2


def test_a_key_from_the_environment_only_serves_the_mode_it_is_set_for(clean, monkeypatch):
    monkeypatch.setenv("CLIPPY_LLM_API_KEY", "env-key-123456")
    assert llm.create_llm("openrouter", storage=clean).api_key == ""
    assert llm.create_llm("openrouter", storage=clean).problem().key == "error.llm_openrouter_key"
    with pytest.raises(ClippyError) as err:
        llm_setup.save(clean, "openrouter")
    assert err.value.key == "error.llm_openrouter_key"
    monkeypatch.setenv("CLIPPY_LLM_BACKEND", "openrouter")
    assert llm.create_llm(storage=clean).api_key == "env-key-123456"
    monkeypatch.setenv("CLIPPY_LLM_BACKEND", "openai")
    monkeypatch.setenv("CLIPPY_LLM_BASE_URL", "https://api.example.com/v1")
    assert llm.create_llm("openrouter", storage=clean).api_key == ""


def test_a_hung_tool_is_killed_even_when_taskkill_fails(monkeypatch):
    killed = []

    class Stubborn:
        pid = 4242

        def communicate(self, data=None, timeout=None):
            raise subprocess.TimeoutExpired("tool", timeout)

        def kill(self):
            killed.append(self.pid)

    monkeypatch.setattr(llm.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(llm.subprocess, "Popen", lambda argv, **kw: Stubborn())
    monkeypatch.setattr(llm.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1))
    monkeypatch.setattr(llm.os, "killpg", lambda *a: (_ for _ in ()).throw(OSError()), raising=False)
    made = llm.build_llm("claude", "")
    made.timeout = 1
    with pytest.raises(ClippyError) as err:
        made.chat_json("s", "u")
    assert err.value.key == "error.llm_cli_timeout" and killed == [4242]


def test_the_cli_timeout_is_read_at_every_call_and_a_wrong_value_means_the_default(monkeypatch):
    assert llm.cli_timeout() == 600
    for value, seconds in (("30", 30), ("abc", 600), ("-5", 600), ("0", 600), ("", 600)):
        monkeypatch.setenv("CLIPPY_LLM_CLI_TIMEOUT", value)
        assert llm.cli_timeout() == seconds


def test_a_hosted_or_cli_backend_never_waits_for_the_gpu(monkeypatch):
    from contextlib import contextmanager

    from clippy import concurrency

    held = []

    class Gpu:
        @contextmanager
        def hold(self):
            held.append(1)
            yield

    monkeypatch.setattr(concurrency, "GPU", Gpu())
    monkeypatch.setattr(llm.shutil, "which", lambda name: None)
    assert llm.OllamaLLM.local and not any(c.local for c in (llm.OpenRouterLLM, llm.ClaudeLLM, llm.CodexLLM, llm.NoLLM))
    for name in ("openrouter", "claude", "codex", "none"):
        with pytest.raises(ClippyError):
            GuardedLLM(llm.build_llm(name, "")).chat_json("s", "u")
    assert held == []

    class Local:
        def chat_json(self, *a, **k):
            return {}

    assert GuardedLLM(Local()).chat_json("s", "u") == {} and held == [1]


def test_every_model_name_and_tool_argument_is_checked(clean):
    for bad in ("two words", "a&b", "a|b", "a%PATH%", 'q"q', "a^b", "a<b", "é"):
        with pytest.raises(ClippyError) as err:
            llm.build_llm("claude", bad)
        assert err.value.key == "error.llm_model_name"
        with pytest.raises(ClippyError):
            llm.create_llm("codex", bad, clean)
    with pytest.raises(ClippyError) as arg:
        llm.build_llm("claude", "").run(["/bin/claude", "x&y"])
    assert arg.value.key == "error.llm_bad_argument"


def test_a_key_must_be_printable_ascii_wherever_it_is_typed(clean):
    for bad in ("short", "has a space1", "tab\tkey12345", "kéy-123456789", "x" * 301):
        with pytest.raises(ClippyError) as err:
            llm_setup.candidate(clean, "openrouter", "m:free", bad)
        assert err.value.key == "error.llm_key_format"
        with pytest.raises(ClippyError):
            llm_setup.save(clean, "openrouter", "m:free", bad)
    assert llm_setup.candidate(clean, "openrouter", "m:free", "sk-or-v1-0123456789").api_key == "sk-or-v1-0123456789"


def test_a_readiness_check_is_reused_for_a_few_seconds_and_runs_once_at_a_time():
    now, calls = [0.0], []
    cache = llm_setup.ProblemCache(ttl=10, clock=lambda: now[0])

    def check():
        calls.append(1)
        return ClippyError(key="error.claude_login")

    assert cache.get(("claude", "", False), check).key == "error.claude_login"
    now[0] = 9
    cache.get(("claude", "", False), check)
    assert len(calls) == 1
    cache.get(("codex", "", False), check)
    assert len(calls) == 2
    now[0] = 11
    cache.get(("claude", "", False), check)
    assert len(calls) == 3
    cache.clear()
    cache.get(("claude", "", False), check)
    assert len(calls) == 4


def test_a_saved_choice_drops_the_remembered_readiness(clean, monkeypatch):
    llm_setup.PROBLEMS.clear()
    monkeypatch.setattr(llm.shutil, "which", lambda name: None)
    assert llm_setup.problem(clean, "claude").key == "error.claude_missing"
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setattr(llm.CliLLM, "run", lambda self, *a, **k: (0, json.dumps({"loggedIn": True}), ""))
    assert llm_setup.problem(clean, "claude").key == "error.claude_missing"
    llm_setup.save(clean, "none")
    assert llm_setup.problem(clean, "claude") is None
    llm_setup.PROBLEMS.clear()


def test_odd_answers_from_the_tools_are_errors_not_crashes(clean, tools, monkeypatch):
    _, queue = tools
    queue.append(FakeProc(out="[1, 2]"))
    with pytest.raises(ClippyError) as listed:
        llm.build_llm("claude", "").chat_json("s", "u")
    assert listed.value.key == "error.llm_failed"

    class Reply:
        def json(self):
            return []

    monkeypatch.setattr(llm.requests, "get", lambda *a, **k: Reply())
    assert llm.build_llm("ollama", "m").problem().key == "error.ollama_no_model"


def test_installing_asks_for_the_ai_only_when_the_install_worked(monkeypatch):
    from clippy import control, shell

    asked = []
    monkeypatch.setattr(llm_setup, "first_run", lambda say: asked.append(1))
    for code in (1, 0):
        monkeypatch.setattr(shell, "install", lambda say, code=code: code)
        assert control._install([]) == code
    assert asked == [1]


def test_model_choices_list_what_a_mode_can_use(tmp_path, monkeypatch):
    from clippy import llm_setup
    from clippy.storage import Storage

    storage = Storage(tmp_path / "data", tmp_path / "library")
    llm_setup._listed.clear()
    monkeypatch.setitem(llm_setup.LISTERS, "ollama", lambda: ["qwen3:8b", "bad name!", "clippy-qwen3:14b-q6k"])
    picks = llm_setup.choices(storage, "ollama")
    assert picks[0] == llm_setup.DEFAULT_MODELS["ollama"] and "qwen3:8b" in picks and "bad name!" not in picks
    assert len(picks) == len(set(picks))
    assert llm_setup.choices(storage, "claude")[:3] == ["sonnet", "opus", "haiku"]

    def down():
        raise llm_setup.requests.ConnectionError("offline")

    llm_setup._listed.clear()
    monkeypatch.setitem(llm_setup.LISTERS, "openrouter", down)
    assert llm_setup.choices(storage, "openrouter") == [llm_setup.DEFAULT_MODELS["openrouter"]]
