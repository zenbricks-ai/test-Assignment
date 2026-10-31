"""Anthropic Messages API via the native SDK (structured output through output_config)."""

from functools import lru_cache
from typing import Any

import anthropic

from pulse.providers.base import Completion, Message, ProviderError


@lru_cache(maxsize=8)
def _client(api_key: str, timeout: float) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2)


class ClaudeProvider:
    def __init__(
        self,
        *,
        label: str,
        model: str,
        api_key: str,
        params: dict[str, Any],
        timeout: float,
        max_output_tokens: int,
    ) -> None:
        self.label = label
        self.model = model
        self.params = params  # merged into output_config, e.g. {"effort": "low"}
        self.max_output_tokens = max_output_tokens  # thinking counts toward it too
        self.client = _client(api_key, timeout)

    def chat(
        self, system: str, messages: list[Message], schema_name: str, schema: dict[str, Any]
    ) -> Completion:
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "system": system,
            "messages": messages,
            "output_config": {"format": {"type": "json_schema", "schema": schema}, **self.params},
        }
        # on a refusal, Anthropic retries server-side on another Claude model first
        resp = self.client.beta.messages.create(
            **request, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
        )

        if resp.stop_reason == "refusal":
            raise ProviderError(f"{self.label} refused: {getattr(resp, 'stop_details', None)}")
        if resp.stop_reason == "max_tokens":
            raise ProviderError(f"{self.label} hit max_output_tokens={self.max_output_tokens}")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        if not text:
            raise ProviderError(f"{self.label} returned no text (stop_reason={resp.stop_reason})")
        return Completion(
            text=text,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            request_id=getattr(resp, "_request_id", None),
        )
