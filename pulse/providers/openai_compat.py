"""Chat Completions endpoints: OpenAI, OpenRouter, Gemini (its OpenAI-compatible API)."""

from functools import lru_cache
from typing import Any

from openai import OpenAI

from pulse.providers.base import Completion, Message, ProviderError


@lru_cache(maxsize=32)
def _client(
    base_url: str | None,
    api_key: str,
    timeout: float,
    headers: tuple,
    organization: str | None,
    project: str | None,
) -> OpenAI:
    client = OpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
        max_retries=2,  # SDK backs off on 429/5xx; after that we fall back to the next provider
        default_headers=dict(headers),
    )
    # The SDK fills these from OPENAI_ORG_ID / OPENAI_PROJECT_ID when None, which would send
    # OpenAI's org/project headers to OpenRouter or Gemini too. Settings already read those
    # vars, so set them explicitly: only the "openai" provider gets them.
    client.organization = organization  # bills the right org / project (staging vs prod)
    client.project = project
    return client


class OpenAICompatProvider:
    def __init__(
        self,
        *,
        label: str,
        model: str,
        api_key: str,
        base_url: str | None,
        params: dict[str, Any],
        timeout: float,
        max_output_tokens: int,
        max_tokens_param: str = "max_completion_tokens",
        headers: dict[str, str] | None = None,
        organization: str | None = None,
        project: str | None = None,
    ) -> None:
        self.label = label
        self.model = model
        self.params = params
        self.max_output_tokens = max_output_tokens
        self.max_tokens_param = max_tokens_param
        self.client = _client(
            base_url,
            api_key,
            timeout,
            tuple(sorted((headers or {}).items())),
            organization,
            project,
        )

    def chat(
        self, system: str, messages: list[Message], schema_name: str, schema: dict[str, Any]
    ) -> Completion:
        kwargs: dict[str, Any] = {
            self.max_tokens_param: self.max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
        }
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, *messages],  # type: ignore[list-item]
            extra_body=self.params or None,  # vendor knobs, e.g. reasoning effort
            **kwargs,
        )
        choice = resp.choices[0] if resp.choices else None
        if choice is not None and choice.finish_reason == "length":
            raise ProviderError(f"{self.label} hit max_output_tokens={self.max_output_tokens}")
        if choice is None or not choice.message.content:
            raise ProviderError(f"{self.label} returned no content")
        usage = resp.usage
        return Completion(
            text=choice.message.content,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            request_id=getattr(resp, "_request_id", None),
        )
