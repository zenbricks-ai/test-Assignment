"""LLM provider factory and the runtime-switchable fallback chain.

The chain is read on every call: data/llm.json (set via PUT /llm) overrides the committed
config/llm.json, so a change takes effect on the next LLM call in every worker, no restart.
New vendor = one adapter module, one line in _REGISTRY, and a <vendor>_api_key setting.
"""

import json
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pulse import store
from pulse.config import Settings, get_settings
from pulse.providers.base import LLMProvider
from pulse.providers.claude import ClaudeProvider
from pulse.providers.openai_compat import OpenAICompatProvider


class ProviderSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    model: str
    params: dict[str, Any] = Field(default_factory=dict)  # vendor knobs, passed through
    # cap on generated tokens (reasoning included): bounds latency and cost; replies are
    # ~300 tokens of JSON, the rest is headroom for reasoning at low effort
    max_output_tokens: int = Field(default=8000, ge=256, le=128_000)

    @field_validator("provider")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in _REGISTRY:
            raise ValueError(f"unknown provider {v!r}; one of {sorted(_REGISTRY)}")
        return v

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


TASKS = ("Enrichment", "Impact")  # output schema names = the two LLM tasks


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chain: list[ProviderSpec] = Field(min_length=1)
    # optional per-task chain, e.g. a cheaper model for Enrichment (summaries) than for Impact
    routes: dict[str, list[ProviderSpec]] = Field(default_factory=dict)

    @field_validator("routes")
    @classmethod
    def _known_tasks(cls, v: dict[str, list[ProviderSpec]]) -> dict[str, list[ProviderSpec]]:
        for task, chain in v.items():
            if task not in TASKS:
                raise ValueError(f"unknown task {task!r}; one of {TASKS}")
            if not chain:
                raise ValueError(f"route {task!r} is empty")
        return v

    def chain_for(self, task: str) -> list[ProviderSpec]:
        return self.routes.get(task) or self.chain


# (base_url, name of the output-cap parameter). OpenAI deprecated "max_tokens" for
# "max_completion_tokens" (reasoning models reject the old one); Gemini still takes "max_tokens".
_OPENAI_LIKE = {
    "openrouter": ("https://openrouter.ai/api/v1", "max_completion_tokens"),
    "openai": (None, "max_completion_tokens"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "max_tokens"),
}


def _openai_like(spec: ProviderSpec, key: str, s: Settings) -> LLMProvider:
    headers = (
        {"HTTP-Referer": s.public_base_url, "X-Title": "AI Pulse"}
        if spec.provider == "openrouter"
        else None
    )
    is_openai = spec.provider == "openai"  # org/project ids mean nothing to other vendors
    base_url, max_tokens_param = _OPENAI_LIKE[spec.provider]
    return OpenAICompatProvider(
        label=spec.label,
        model=spec.model,
        api_key=key,
        base_url=base_url,
        params=spec.params,
        timeout=s.llm_timeout_s,
        max_output_tokens=spec.max_output_tokens,
        max_tokens_param=max_tokens_param,
        headers=headers,
        organization=(s.openai_org_id or None) if is_openai else None,
        project=(s.openai_project_id or None) if is_openai else None,
    )


def _claude(spec: ProviderSpec, key: str, s: Settings) -> LLMProvider:
    return ClaudeProvider(
        label=spec.label,
        model=spec.model,
        api_key=key,
        params=spec.params,
        timeout=s.llm_timeout_s,
        max_output_tokens=spec.max_output_tokens,
    )


_REGISTRY: dict[str, Callable[[ProviderSpec, str, Settings], LLMProvider]] = {
    "openrouter": _openai_like,
    "openai": _openai_like,
    "gemini": _openai_like,
    "anthropic": _claude,
}
PROVIDERS = tuple(_REGISTRY)


def api_key(spec: ProviderSpec) -> str:
    """<PROVIDER>_API_KEY from .env or the environment; "" if unset."""
    return str(getattr(get_settings(), f"{spec.provider}_api_key").get_secret_value())


def create_provider(spec: ProviderSpec) -> LLMProvider:
    return _REGISTRY[spec.provider](spec, api_key(spec), get_settings())


def active_config() -> tuple[LLMConfig, str]:
    """(config, source) - runtime override if one was saved, else the committed default."""
    if (override := store.load("llm")) is not None:
        return LLMConfig.model_validate(override), "override"
    path = get_settings().config_dir / "llm.json"
    return LLMConfig.model_validate(json.loads(path.read_text("utf-8"))), "default"


def save_override(config: LLMConfig) -> None:
    store.save("llm", config.model_dump(exclude_defaults=True))


def clear_override() -> None:
    store.delete("llm")
