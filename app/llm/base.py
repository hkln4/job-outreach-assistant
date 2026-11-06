"""
Provider-agnostic LLM interface (section 8). Business logic must only ever
call LLMProvider.generate(...) - never a provider SDK directly.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    raw: dict | None = None  # provider's raw response, kept for debugging only
    # Token usage as the provider reported it (0 when it didn't) - recorded in
    # LLMCallLog so the dashboard can show where the money goes.
    input_tokens: int = 0
    output_tokens: int = 0


class LLMProvider(ABC):
    @abstractmethod
    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.2,
        max_output_tokens: int = 2048,
        json_schema: dict | None = None,
    ) -> LLMResponse:
        """Send one prompt, return one response. No streaming needed for this app's use cases
        (per-item extraction, preference checks, cover-letter drafts).

        `json_schema` (a plain JSON Schema, e.g. `Model.model_json_schema()`)
        asks the provider to constrain the answer to that shape with its native
        structured-output mode. Callers still parse and validate the text - the
        schema makes a malformed answer rarer, it doesn't replace the check."""
        raise NotImplementedError
