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


class LLMProvider(ABC):
    @abstractmethod
    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.2,
        max_output_tokens: int = 2048,
    ) -> LLMResponse:
        """Send one prompt, return one response. No streaming needed for this app's use cases
        (per-item extraction, preference checks, cover-letter drafts)."""
        raise NotImplementedError
