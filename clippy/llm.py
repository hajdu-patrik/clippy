"""LLM backends behind one interface: local Ollama, OpenRouter or any OpenAI-compatible API, the user's Claude or
ChatGPT subscription through the vendor's own command line tool, or none. The mode the user picked lives in
data/web/llm.json and is read at every call, so a change takes effect at once."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

import requests

from .storage import Storage
from .tools import ClippyError, read_json

OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "clippy-qwen3:14b-q6k"
OPENROUTER_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = "qwen/qwen3-235b-a22b:free"
MODES = ("ollama", "openrouter", "claude", "codex", "none")
DEFAULT_MODELS = {"ollama": DEFAULT_MODEL, "openrouter": OPENROUTER_MODEL}
MODEL_NAME = re.compile(r"[\w./:@+-]{0,100}", re.ASCII)
UNSAFE_ARGUMENT = re.compile(r'[%^&|<>"]')
CLI_TIMEOUT = 600
CLI_ENV = frozenset(("PATH PATHEXT SYSTEMROOT SYSTEMDRIVE COMSPEC WINDIR USERPROFILE HOMEDRIVE HOMEPATH HOME APPDATA "
                     "LOCALAPPDATA PROGRAMDATA TEMP TMP USERNAME LANG CLAUDE_CONFIG_DIR CODEX_HOME HTTP_PROXY "
                     "HTTPS_PROXY NO_PROXY").split())
CLI_ENV_PREFIXES = ("PROGRAMFILES", "LC_")
OLLAMA_SERVER_ENV = {"OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KV_CACHE_TYPE": "q8_0",
                     "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_MAX_LOADED_MODELS": "1"}


def setting(name: str, default: str = "") -> str:
    return os.environ.get(f"CLIPPY_LLM_{name}") or default


def think_enabled() -> bool:
    return setting("THINK", "1") != "0"


def cli_timeout() -> int:
    """Seconds a command line tool may take, read at every call; a wrong value means the default."""
    try:
        seconds = int(setting("CLI_TIMEOUT", str(CLI_TIMEOUT)))
    except ValueError:
        return CLI_TIMEOUT
    return seconds if seconds > 0 else CLI_TIMEOUT


def cli_env() -> dict[str, str]:
    """What a command line tool gets of the server's environment: the system basics, the tool's own home and the
    proxy; never a Clippy setting or any API key."""
    env = {k: v for k, v in os.environ.items() if k.upper() in CLI_ENV or k.upper().startswith(CLI_ENV_PREFIXES)}
    return {**env, "NO_COLOR": "1"}


def stored(storage: Storage | None = None) -> dict:
    """The user's choice from Settings or `clippy llm`: {"mode", "models": {mode: name}, "keys": {mode: key}}."""
    try:
        data = read_json((storage or Storage()).llm_file)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def strict_schema(schema) -> bool:
    """Whether a JSON Schema meets the strict structured-output rules (every object closed and fully required)."""
    if isinstance(schema, list):
        return all(strict_schema(item) for item in schema)
    if not isinstance(schema, dict):
        return True
    if schema.get("type") == "object" and (schema.get("additionalProperties") is not False
                                           or set(schema.get("required") or ()) != set(schema.get("properties") or ())):
        return False
    return all(strict_schema(value) for value in schema.values())


def json_prompt(user: str, schema: dict | None) -> str:
    """The question for a model that cannot be forced to answer in JSON: it is told the shape in words."""
    ask = "Reply with one JSON object only, without markdown fences or any other text."
    if schema:
        ask += " It must match this JSON Schema:\n" + json.dumps(schema, ensure_ascii=False)
    return f"{user}\n\n{ask}"


class LLM(ABC):
    backend = ""
    local = False

    def __init__(self, model: str, base_url: str = "", api_key: str = ""):
        self.model, self.base_url, self.api_key = model, base_url.rstrip("/"), api_key

    def describe(self) -> str:
        return f"{self.backend}:{self.model}"

    def chat_json(self, system: str, user: str, think: bool = False, schema: dict | None = None) -> dict:
        return parse_json_reply(self._complete(system, user, think, schema))

    def unload(self) -> None:
        return None

    def warm(self) -> None:
        """Load the model ahead of the first request (a cold 14B model takes about a minute)."""
        return None

    def problem(self) -> ClippyError | None:
        """Why this backend cannot answer right now (not installed, not signed in, no key), else None."""
        return None

    @abstractmethod
    def _complete(self, system: str, user: str, think: bool, schema: dict | None) -> str: ...


class OllamaLLM(LLM):
    backend = "ollama"
    local = True
    SAMPLING = {
        True: {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0},
        False: {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0},
    }

    def options(self, think: bool) -> dict:
        return {**self.SAMPLING[think], "presence_penalty": 1.5, "repeat_penalty": 1.0,
                "num_ctx": int(setting("CTX", "16384")), "num_predict": 8192 if think else 2048}

    def _complete(self, system: str, user: str, think: bool, schema: dict | None) -> str:
        payload = {
            "model": self.model, "stream": False, "think": think, "format": schema or "json",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "keep_alive": setting("KEEP_ALIVE", "30m"), "options": self.options(think),
        }
        r = self._chat(payload)
        if r.status_code == 400 and "think" in r.text.lower():
            payload.pop("think")
            payload["messages"][0]["content"] += "\n/think" if think else "\n/no_think"
            r = self._chat(payload)
        if r.status_code == 404:
            raise ClippyError(key="error.ollama_no_model", model=self.model)
        if r.status_code == 500 and "memory" in r.text.lower():
            raise ClippyError(key="error.llm_vram", model=self.model, detail=r.text[:200])
        if not r.ok:
            raise ClippyError(key="error.llm_api", status=r.status_code, detail=r.text[:300])
        return _answer(r, "message", "content")

    def _chat(self, payload: dict) -> requests.Response:
        try:
            return requests.post(f"{self.base_url}/api/chat", json=payload, timeout=1800)
        except requests.ConnectionError as exc:
            raise ClippyError(key="error.ollama_unreachable", url=self.base_url, model=self.model) from exc
        except requests.RequestException as exc:
            raise _failed(exc) from exc

    def problem(self) -> ClippyError | None:
        try:
            body = requests.get(f"{self.base_url}/api/tags", timeout=3).json()
        except (requests.RequestException, ValueError):
            return ClippyError(key="error.ollama_unreachable", url=self.base_url, model=self.model)
        models = body.get("models") if isinstance(body, dict) else None
        names = {m.get("name") for m in models or [] if isinstance(m, dict)}
        if self.model in names or f"{self.model}:latest" in names:
            return None
        return ClippyError(key="error.ollama_no_model", model=self.model)

    def unload(self) -> None:
        _signal(f"{self.base_url}/api/generate", {"model": self.model, "keep_alive": 0})

    def warm(self) -> None:
        _signal(f"{self.base_url}/api/generate", {"model": self.model, "keep_alive": setting("KEEP_ALIVE", "30m")},
                timeout=300)


class OpenAICompatibleLLM(LLM):
    backend = "openai"
    extra_headers: dict[str, str] = {}

    def _complete(self, system: str, user: str, think: bool, schema: dict | None) -> str:
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {"model": self.model, "temperature": 0.4, "response_format": {"type": "json_object"},
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        url = f"{self.base_url}/chat/completions"
        r = self._post(url, payload, headers)
        if r.status_code == 400 and "response_format" in r.text:
            payload.pop("response_format")
            r = self._post(url, payload, headers)
        if r.status_code == 429:
            raise ClippyError(key="error.llm_quota")
        if r.status_code in (401, 403):
            raise ClippyError(key="error.llm_auth", status=r.status_code)
        if not r.ok:
            raise ClippyError(key="error.llm_api", status=r.status_code, detail=r.text[:300])
        return _answer(r, "choices", 0, "message", "content")

    @staticmethod
    def _post(url: str, payload: dict, headers: dict) -> requests.Response:
        try:
            return requests.post(url, json=payload, headers=headers, timeout=300)
        except requests.RequestException as exc:
            raise _failed(exc) from exc


class OpenRouterLLM(OpenAICompatibleLLM):
    """OpenRouter's OpenAI-compatible API with the user's key; its ":free" models cost nothing."""

    backend = "openrouter"
    extra_headers = {"X-Title": "Clippy"}

    def problem(self) -> ClippyError | None:
        return None if self.api_key else ClippyError(key="error.llm_openrouter_key")

    def _complete(self, system: str, user: str, think: bool, schema: dict | None) -> str:
        if not self.api_key:
            raise ClippyError(key="error.llm_openrouter_key")
        return super()._complete(system, json_prompt(user, schema), think, None)


class CliLLM(LLM):
    """A subscription used through the vendor's own command line tool, signed in by the user, with no API key."""

    tool = ""
    timeout: int | None = None

    def describe(self) -> str:
        return f"{self.backend}:{self.model or 'default'}"

    def executable(self) -> str:
        found = shutil.which(self.tool)
        if not found:
            raise ClippyError(key=f"error.{self.backend}_missing")
        return found

    def problem(self) -> ClippyError | None:
        try:
            return self._sign_in_problem(self.executable())
        except ClippyError as exc:
            return exc

    def run(self, argv: list[str], text: str = "", timeout: float | None = None,
            cwd: str | None = None) -> tuple[int, str, str]:
        """Run the tool without a shell and without a window; a tool that hangs is stopped with its children."""
        for arg in argv[1:]:
            if UNSAFE_ARGUMENT.search(arg):
                raise ClippyError(key="error.llm_bad_argument", arg=arg[:40])
        extra = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {"start_new_session": True}
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    cwd=cwd, env=cli_env(), **extra)
        except OSError as exc:
            raise ClippyError(key=f"error.{self.backend}_missing") from exc
        wait = timeout or self.timeout or cli_timeout()
        try:
            out, err = proc.communicate(text.encode("utf-8"), timeout=wait)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            raise ClippyError(key="error.llm_cli_timeout", tool=self.tool, seconds=int(wait)) from None
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def refused(self, detail: str) -> ClippyError:
        """The error for a tool that ran and said no: its usage limit, a missing sign-in, or anything else."""
        if re.search(r"usage limit|rate limit|limit reached|quota", detail, re.I):
            return ClippyError(key="error.llm_cli_limit", tool=self.tool, detail=detail[:200])
        if re.search(r"not logged in|log ?in|sign ?in|unauthori[sz]ed|authenticat|credentials|401", detail, re.I):
            return ClippyError(key=f"error.{self.backend}_login")
        return ClippyError(key="error.llm_failed", detail=detail[:300])

    @abstractmethod
    def _sign_in_problem(self, exe: str) -> ClippyError | None: ...


class ClaudeLLM(CliLLM):
    """Claude Code in print mode: no tools, no saved session, no settings files, its own system prompt."""

    backend = "claude"
    tool = "claude"

    def _sign_in_problem(self, exe: str) -> ClippyError | None:
        code, out, _ = self.run([exe, "auth", "status"], timeout=20)
        try:
            signed_in = bool(json.loads(out).get("loggedIn"))
        except (ValueError, AttributeError):
            signed_in = code == 0
        return None if signed_in else ClippyError(key="error.claude_login")

    def _complete(self, system: str, user: str, think: bool, schema: dict | None) -> str:
        exe = self.executable()
        with tempfile.TemporaryDirectory(prefix="clippy-", ignore_cleanup_errors=True) as scratch:
            prompt = Path(scratch, "system.txt")
            prompt.write_text(system, encoding="utf-8")
            argv = [exe, "-p", "--output-format", "json", "--tools", "", "--no-session-persistence",
                    "--disable-slash-commands", "--setting-sources", "", "--strict-mcp-config",
                    "--system-prompt-file", str(prompt)]
            if self.model:
                argv += ["--model", self.model]
            code, out, err = self.run(argv, json_prompt(user, schema), cwd=scratch)
        try:
            reply = json.loads(out)
        except ValueError:
            raise self.refused(err or out) from None
        if not isinstance(reply, dict):
            raise self.refused(err or out)
        if code != 0 or reply.get("is_error"):
            raise self.refused(str(reply.get("result") or err or out))
        return str(reply.get("result") or "")


class CodexLLM(CliLLM):
    """Codex in exec mode: read-only sandbox, no saved session, no user config, and every tool that reaches beyond
    the question switched off (shell, apps, plugins, browser, computer, search). The last message is the answer.
    Signed out, `codex exec` waits for a login that never comes, so the sign-in is checked first."""

    backend = "codex"
    tool = "codex"
    FEATURES = ("apps", "plugins", "remote_plugin", "browser_use", "computer_use", "shell_tool", "image_generation",
                "multi_agent")

    def _sign_in_problem(self, exe: str) -> ClippyError | None:
        code, out, _ = self.run([exe, "login", "status"], timeout=20)
        if code != 0:
            return ClippyError(key="error.codex_login")
        return None if "chatgpt" in out.lower() else ClippyError(key="error.codex_not_subscription")

    def _complete(self, system: str, user: str, think: bool, schema: dict | None) -> str:
        exe = self.executable()
        problem = self._sign_in_problem(exe)
        if problem is not None:
            raise problem
        with tempfile.TemporaryDirectory(prefix="clippy-", ignore_cleanup_errors=True) as scratch:
            answer = Path(scratch, "answer.txt")
            argv = [exe, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--ephemeral",
                    "--ignore-user-config", "--ignore-rules", "--color", "never", "-c", "web_search=disabled",
                    "--output-last-message", str(answer)]
            for feature in self.FEATURES:
                argv += ["--disable", feature]
            if schema and strict_schema(schema):
                shape = Path(scratch, "schema.json")
                shape.write_text(json.dumps(schema), encoding="utf-8")
                argv += ["--output-schema", str(shape)]
            if self.model:
                argv += ["--model", self.model]
            prompt = (f"{system}\n\n---\n\n{json_prompt(user, schema)}\n\n"
                      "Answer directly: run no commands and read no files.")
            code, out, err = self.run(argv + ["-"], prompt, cwd=scratch)
            text = answer.read_text(encoding="utf-8").strip() if answer.is_file() else ""
        if code != 0 or not text:
            lines = [ln[len("ERROR:"):].strip() for ln in err.splitlines() if ln.startswith("ERROR:")]
            raise self.refused(lines[-1] if lines else err or out)
        return text


class NoLLM(LLM):
    backend = "none"

    def describe(self) -> str:
        return "heuristic (no LLM)"

    def _complete(self, system: str, user: str, think: bool, schema: dict | None) -> str:
        raise ClippyError(key="error.llm_none")


BACKENDS: dict[str, type[LLM]] = {
    c.backend: c for c in (OllamaLLM, OpenAICompatibleLLM, OpenRouterLLM, ClaudeLLM, CodexLLM, NoLLM)}


def mode_of(config: dict, backend: str | None = None) -> str:
    """The backend in force: the one asked for, else the user's choice, else CLIPPY_LLM_BACKEND, else Ollama."""
    return (backend or config.get("mode") or setting("BACKEND", "ollama")).lower()


def model_of(name: str, config: dict) -> str:
    """The user's model for a mode, else the environment's (when that is the mode it configures), else the default."""
    env = setting("MODEL") if name == setting("BACKEND", "ollama").lower() else ""
    return (config.get("models") or {}).get(name) or env or DEFAULT_MODELS.get(name, "")


def key_of(name: str, config: dict) -> str:
    """The user's key for a mode, else the environment's (only for the mode CLIPPY_LLM_BACKEND configures)."""
    env = setting("API_KEY") if name in ("openai", "openrouter") and name == setting("BACKEND").lower() else ""
    return (config.get("keys") or {}).get(name) or env


def build_llm(name: str, model: str, key: str = "") -> LLM:
    if name not in BACKENDS:
        raise ClippyError(key="error.llm_backend", name=name, choices=", ".join(MODES))
    if not MODEL_NAME.fullmatch(model):
        raise ClippyError(key="error.llm_model_name")
    base_url = {"ollama": setting("BASE_URL", OLLAMA_URL), "openai": setting("BASE_URL"),
                "openrouter": OPENROUTER_URL}.get(name, "")
    if name == "openai" and not base_url:
        raise ClippyError(key="error.llm_base_url")
    return BACKENDS[name](model, base_url, key)


def create_llm(backend: str | None = None, model: str | None = None, storage: Storage | None = None) -> LLM:
    config = stored(storage)
    name = mode_of(config, backend)
    return build_llm(name, model or model_of(name, config), key_of(name, config))


def _kill_tree(proc: subprocess.Popen) -> None:
    """Stop a tool and everything it started (the npm shims start node under cmd.exe)."""
    try:
        if sys.platform == "win32":
            done = subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                                  creationflags=subprocess.CREATE_NO_WINDOW, timeout=20)
            if done.returncode == 0:
                return
        else:
            os.killpg(proc.pid, signal.SIGKILL)
            return
    except (OSError, subprocess.SubprocessError):
        pass
    proc.kill()


def _failed(exc: Exception) -> ClippyError:
    """A request that broke off (a timeout, a dropped connection): an error the optional LLM steps can skip."""
    return ClippyError(key="error.llm_failed", detail=f"{type(exc).__name__}: {exc}"[:300])


def _answer(response: requests.Response, *path) -> str:
    """The text at `path` in a model's JSON answer; a malformed answer is an LLM error too."""
    try:
        value = response.json()
        for step in path:
            value = value[step]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ClippyError(key="error.llm_failed", detail=response.text[:300]) from exc
    return str(value or "")


def _signal(url: str, data: dict, timeout: float = 30) -> None:
    """A request whose answer does not matter (loading or unloading a model)."""
    try:
        requests.post(url, json=data, timeout=timeout)
    except requests.RequestException:
        pass


def ollama_running(base_url: str = OLLAMA_URL) -> bool:
    try:
        return requests.get(f"{base_url}/api/version", timeout=2).ok
    except requests.RequestException:
        return False


def loaded_models(base_url: str = OLLAMA_URL) -> list[dict] | None:
    try:
        return requests.get(f"{base_url}/api/ps", timeout=2).json().get("models", [])
    except (requests.RequestException, ValueError):
        return None


def unload_all(base_url: str = OLLAMA_URL) -> list[str]:
    names = [m["name"] for m in loaded_models(base_url) or []]
    for name in names:
        _signal(f"{base_url}/api/generate", {"model": name, "keep_alive": 0})
    return names


def parse_json_reply(text: str) -> dict:
    """Tolerant JSON extraction: drops <think> blocks and ``` fences, then takes the outer object."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    raise ClippyError(key="error.llm_json", detail=text[:500])
