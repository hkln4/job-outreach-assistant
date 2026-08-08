"""Test doubles - a canned-response LLM provider so business logic and routes can
be exercised without a real API key or network call to an LLM."""
from app.llm.base import LLMProvider, LLMResponse


class FakeLLMProvider(LLMProvider):
    def __init__(self, text: str):
        self._text = text
        self.calls: list[dict] = []

    def generate(self, prompt, *, system=None, temperature=0.2, max_output_tokens=2048):
        self.calls.append({"prompt": prompt, "system": system})
        return LLMResponse(text=self._text, model="fake", provider="fake")


class SequencedLLMProvider(LLMProvider):
    """Returns a different canned response on each successive call."""

    def __init__(self, texts: list[str]):
        self._texts = list(texts)
        self.calls: list[dict] = []

    def generate(self, prompt, *, system=None, temperature=0.2, max_output_tokens=2048):
        self.calls.append({"prompt": prompt, "system": system})
        text = self._texts.pop(0) if self._texts else "{}"
        return LLMResponse(text=text, model="fake", provider="fake")
