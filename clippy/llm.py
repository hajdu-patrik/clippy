"""LLM backends behind one interface: local Ollama, any OpenAI-compatible API, or none."""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod

import requests

from .tools import ClippyError

OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "clippy-qwen3:14b-q6k"
OLLAMA_SERVER_ENV = {"OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KV_CACHE_TYPE": "q8_0",
                     "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_MAX_LOADED_MODELS": "1"}


def setting(name: str, default: str = "") -> str:
    return os.environ.get(f"CLIPPY_LLM_{name}") or default


def think_enabled() -> bool:
    return setting("THINK", "1") != "0"


class LLM(ABC):
    backend = ""

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

    @abstractmethod
    def _complete(self, system: str, user: str, think: bool, schema: dict | None) -> str: ...


class OllamaLLM(LLM):
    backend = "ollama"
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
        try:
            r = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=1800)
        except requests.ConnectionError as exc:
            raise ClippyError(f"Ollama is not reachable at {self.base_url}. Start the Ollama app "
                              f"and pull the model: ollama pull {self.model}") from exc
        if r.status_code == 400 and "think" in r.text.lower():
            payload.pop("think")
            payload["messages"][0]["content"] += "\n/think" if think else "\n/no_think"
            r = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=1800)
        if r.status_code == 404:
            raise ClippyError(f"Ollama has no model '{self.model}'. Run: ollama pull {self.model}")
        if r.status_code == 500 and "memory" in r.text.lower():
            raise ClippyError(f"Not enough VRAM for {self.model}: {r.text[:200]}")
        r.raise_for_status()
        return r.json()["message"]["content"]

    def unload(self) -> None:
        _post(f"{self.base_url}/api/generate", {"model": self.model, "keep_alive": 0})

    def warm(self) -> None:
        _post(f"{self.base_url}/api/generate", {"model": self.model, "keep_alive": setting("KEEP_ALIVE", "30m")},
              timeout=300)


class OpenAICompatibleLLM(LLM):
    backend = "openai"

    def _complete(self, system: str, user: str, think: bool, schema: dict | None) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {"model": self.model, "temperature": 0.4, "response_format": {"type": "json_object"},
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        url = f"{self.base_url}/chat/completions"
        r = requests.post(url, json=payload, headers=headers, timeout=300)
        if r.status_code == 400 and "response_format" in r.text:
            payload.pop("response_format")
            r = requests.post(url, json=payload, headers=headers, timeout=300)
        if r.status_code == 429:
            raise ClippyError("The free API quota is used up (429). Wait a bit or switch to Ollama.")
        if not r.ok:
            raise ClippyError(f"LLM API error {r.status_code}: {r.text[:300]}")
        return r.json()["choices"][0]["message"]["content"]


class NoLLM(LLM):
    backend = "none"

    def describe(self) -> str:
        return "heuristic (no LLM)"

    def _complete(self, system: str, user: str, think: bool, schema: dict | None) -> str:
        raise ClippyError("No LLM is configured (CLIPPY_LLM_BACKEND=none).")


BACKENDS: dict[str, type[LLM]] = {c.backend: c for c in (OllamaLLM, OpenAICompatibleLLM, NoLLM)}


def create_llm(backend: str | None = None, model: str | None = None) -> LLM:
    name = (backend or setting("BACKEND", "ollama")).lower()
    if name not in BACKENDS:
        raise ClippyError(f"Unknown LLM backend '{name}' (choose: {', '.join(BACKENDS)}).")
    base_url = setting("BASE_URL", OLLAMA_URL if name == "ollama" else "")
    if name == "openai" and not base_url:
        raise ClippyError("The openai backend needs CLIPPY_LLM_BASE_URL (e.g. a Groq / Gemini / OpenRouter URL).")
    return BACKENDS[name](model or setting("MODEL", DEFAULT_MODEL), base_url, setting("API_KEY"))


def _post(url: str, data: dict, timeout: float = 30) -> None:
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
        _post(f"{base_url}/api/generate", {"model": name, "keep_alive": 0})
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
    raise ClippyError(f"The LLM did not return valid JSON:\n{text[:500]}")
