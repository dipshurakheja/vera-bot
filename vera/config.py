"""Runtime configuration, read once from environment variables.

Every value has a safe default so the server runs with zero configuration
(deterministic composer, no LLM). See `.env.example` for the full list.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value.strip() if value is not None else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "").lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # --- identity served by /v1/metadata ---
    team_name: str = "Team Vera"
    team_members: tuple[str, ...] = ("Vera Engine",)
    contact_email: str = "team@example.com"
    version: str = "1.0.0"
    submitted_at: str = "2026-09-25T00:00:00Z"
    approach: str = (
        "Deterministic decision engine: signal extraction over the 4 contexts, "
        "trigger-kind strategies, consent/suppression/cooldown gates, ranked single-CTA "
        "composition with number-grounding validation; optional LLM polish "
        "(cached, validated, deterministic fallback)."
    )

    # --- optional LLM polish layer ---
    llm_provider: str = ""          # "", anthropic, openai, openai_compatible, groq, deepseek, openrouter
    llm_api_key: str = field(default="", repr=False)
    llm_model: str = ""
    llm_base_url: str = ""
    llm_mode: str = "off"           # off | polish
    llm_timeout_s: float = 12.0
    llm_max_retries: int = 1
    tick_llm_budget_s: float = 18.0

    # --- engine limits / policy ---
    max_actions_per_tick: int = 20
    max_context_bytes: int = 512 * 1024
    max_request_bytes: int = 64 * 1024
    enforce_trigger_expiry: bool = False
    opt_out_days: int = 30
    merchant_cooldown_minutes: int = 30
    log_level: str = "INFO"

    @property
    def llm_enabled(self) -> bool:
        return self.llm_mode == "polish" and bool(self.llm_provider) and (
            bool(self.llm_api_key) or self.llm_provider in {"ollama"}
        )

    @property
    def model_label(self) -> str:
        if self.llm_enabled:
            return f"{self.llm_provider}:{self.llm_model or 'default'} (polish) + deterministic composer"
        return "deterministic-composer-v1 (no LLM in the decision path)"

    @classmethod
    def from_env(cls) -> "Settings":
        members = tuple(m.strip() for m in _env("TEAM_MEMBERS", "Vera Engine").split(",") if m.strip())
        provider = _env("LLM_PROVIDER").lower()
        api_key = _env("LLM_API_KEY")
        default_mode = "polish" if provider and (api_key or provider == "ollama") else "off"
        mode = _env("VERA_LLM_MODE", default_mode).lower()
        if mode not in {"off", "polish"}:
            mode = "off"
        return cls(
            team_name=_env("TEAM_NAME", cls.team_name),
            team_members=members or cls.team_members,
            contact_email=_env("CONTACT_EMAIL", cls.contact_email),
            version=_env("BOT_VERSION", cls.version),
            submitted_at=_env("SUBMITTED_AT", cls.submitted_at),
            approach=_env("BOT_APPROACH", cls.approach),
            llm_provider=provider,
            llm_api_key=api_key,
            llm_model=_env("LLM_MODEL", _env("MODEL")),
            llm_base_url=_env("LLM_BASE_URL"),
            llm_mode=mode,
            llm_timeout_s=_env_float("LLM_TIMEOUT_SECONDS", cls.llm_timeout_s),
            llm_max_retries=max(0, _env_int("LLM_MAX_RETRIES", cls.llm_max_retries)),
            tick_llm_budget_s=_env_float("VERA_TICK_LLM_BUDGET_SECONDS", cls.tick_llm_budget_s),
            max_actions_per_tick=max(1, min(20, _env_int("VERA_MAX_ACTIONS_PER_TICK", 20))),
            max_context_bytes=_env_int("VERA_MAX_CONTEXT_BYTES", cls.max_context_bytes),
            max_request_bytes=_env_int("VERA_MAX_REQUEST_BYTES", cls.max_request_bytes),
            enforce_trigger_expiry=_env_bool("VERA_ENFORCE_TRIGGER_EXPIRY", False),
            opt_out_days=_env_int("VERA_OPT_OUT_DAYS", cls.opt_out_days),
            merchant_cooldown_minutes=max(0, _env_int("VERA_MERCHANT_COOLDOWN_MINUTES", cls.merchant_cooldown_minutes)),
            log_level=_env("LOG_LEVEL", cls.log_level).upper(),
        )
