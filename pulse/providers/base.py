from dataclasses import dataclass
from typing import Any, Protocol

Message = dict[str, str]  # {"role": "user" | "assistant", "content": str}


class ProviderError(Exception):
    """Provider answered but unusably (refusal, truncation, empty output). Triggers fallback."""


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    request_id: str | None = None  # vendor request id: quote it in support tickets

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class LLMProvider(Protocol):
    """Same input and output for every vendor: prompts in, JSON text + usage out."""

    label: str  # "<provider>:<model>", for logs

    def chat(
        self, system: str, messages: list[Message], schema_name: str, schema: dict[str, Any]
    ) -> Completion: ...
