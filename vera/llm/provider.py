"""LLM provider abstraction.

The engine never *needs* an LLM: every provider failure (missing key, timeout,
refusal, malformed output) surfaces as LLMError and callers fall back to the
deterministic composer.

Determinism notes
  * OpenAI-compatible providers: temperature=0 + fixed seed.
  * Anthropic: current models (Opus 5, Fable 5.x, Sonnet 5, Opus 4.7+) reject
    sampling parameters, so determinism comes from (a) the in-process cache in
    polish.py keyed by the full prompt and (b) strict output validation with a
    deterministic fallback. `temperature=0` is sent only to older models that
    still accept it.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from typing import Optional
from urllib import error as urlerror
from urllib import request as urlrequest

from ..config import Settings
from ..utils.logging import get_logger, log_event

log = get_logger("llm")


class LLMError(Exception):
    """Any provider failure. Message never contains secrets."""


class LLMProvider(ABC):
    name: str = "base"
    model: str = ""

    @abstractmethod
    def _call(self, prompt: str, system: str, max_tokens: int, timeout: float) -> str: ...

    def __init__(self, timeout: float = 12.0, max_retries: int = 1) -> None:
        self.timeout = timeout
        self.max_retries = max_retries

    def generate(self, prompt: str, system: str = "", max_tokens: int = 1024) -> str:
        """Call the model with bounded retries; raises LLMError on final failure."""
        last: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            started = time.perf_counter()
            try:
                text = self._call(prompt, system, max_tokens, self.timeout)
                if not isinstance(text, str) or not text.strip():
                    raise LLMError("empty completion")
                return text
            except LLMError as exc:
                last = exc
            except Exception as exc:  # network / SDK errors — never leak details upstream
                last = LLMError(type(exc).__name__)
            log_event(log, "llm_attempt_failed", provider=self.name, attempt=attempt + 1,
                      latency_ms=int((time.perf_counter() - started) * 1000), error=str(last))
            if attempt < self.max_retries:
                time.sleep(min(0.5 * (2 ** attempt), 2.0))
        raise LLMError(str(last) if last else "unknown failure")


class NullProvider(LLMProvider):
    name = "none"

    def _call(self, prompt: str, system: str, max_tokens: int, timeout: float) -> str:
        raise LLMError("LLM disabled")


_SAMPLING_OK = ("claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-6", "claude-sonnet-4-5", "claude-opus-4-5")
_FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5-1")


class AnthropicProvider(LLMProvider):
    """Claude via the official `anthropic` SDK (imported lazily — optional dependency)."""

    name = "anthropic"

    def __init__(self, api_key: str, model: str = "", base_url: str = "", timeout: float = 12.0,
                 max_retries: int = 1) -> None:
        super().__init__(timeout, max_retries)
        self.model = model or "claude-opus-5"
        try:
            import anthropic  # noqa: WPS433 (optional dependency)
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise LLMError("anthropic SDK not installed (pip install anthropic)") from exc
        kwargs = {"api_key": api_key, "timeout": timeout, "max_retries": 0}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = anthropic.Anthropic(**kwargs)

    def _call(self, prompt: str, system: str, max_tokens: int, timeout: float) -> str:
        params = {
            "model": self.model,
            "max_tokens": max(max_tokens, 2048),
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            params["system"] = system
        if self.model.startswith(_SAMPLING_OK):
            params["temperature"] = 0
        else:
            params["output_config"] = {"effort": "low"}  # short, routine rewrite
        if self.model in _FALLBACK_MODELS:
            response = self._client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
        else:
            response = self._client.messages.create(**params)
        if getattr(response, "stop_reason", None) == "refusal":
            raise LLMError("model refused")
        texts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
        return "".join(texts)


_OPENAI_COMPAT = {
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "openrouter": ("https://openrouter.ai/api/v1", "anthropic/claude-opus-5"),
    "ollama": ("http://localhost:11434/v1", "llama3"),
    "openai_compatible": ("", ""),
}


class OpenAICompatibleProvider(LLMProvider):
    """Any /chat/completions endpoint (OpenAI, Groq, DeepSeek, OpenRouter, Ollama, vLLM...)."""

    def __init__(self, provider: str, api_key: str, model: str = "", base_url: str = "",
                 timeout: float = 12.0, max_retries: int = 1) -> None:
        super().__init__(timeout, max_retries)
        default_url, default_model = _OPENAI_COMPAT.get(provider, ("", ""))
        self.name = provider
        self.base_url = (base_url or default_url).rstrip("/")
        self.model = model or default_model
        self._key = api_key
        if not self.base_url or not self.model:
            raise LLMError(f"{provider}: LLM_BASE_URL and LLM_MODEL are required")

    def _call(self, prompt: str, system: str, max_tokens: int, timeout: float) -> str:
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        body = json.dumps({"model": self.model, "messages": messages, "temperature": 0, "seed": 7,
                           "max_tokens": max_tokens}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        req = urlrequest.Request(f"{self.base_url}/chat/completions", data=body, headers=headers, method="POST")
        try:
            with urlrequest.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urlerror.HTTPError as exc:
            raise LLMError(f"HTTP {exc.code}") from None
        except urlerror.URLError as exc:
            raise LLMError(f"connection error: {type(exc.reason).__name__}") from None
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise LLMError("unexpected response shape") from None


def build_provider(settings: Settings) -> Optional[LLMProvider]:
    """Provider for the configured LLM, or None when the LLM layer is off/misconfigured."""
    if not settings.llm_enabled:
        return None
    try:
        if settings.llm_provider in {"anthropic", "claude"}:
            return AnthropicProvider(settings.llm_api_key, settings.llm_model, settings.llm_base_url,
                                     settings.llm_timeout_s, settings.llm_max_retries)
        return OpenAICompatibleProvider(settings.llm_provider, settings.llm_api_key, settings.llm_model,
                                        settings.llm_base_url, settings.llm_timeout_s, settings.llm_max_retries)
    except LLMError as exc:
        log_event(log, "llm_provider_unavailable", provider=settings.llm_provider, error=str(exc))
        return None
