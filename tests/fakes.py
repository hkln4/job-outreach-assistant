"""Test doubles - a canned-response LLM provider so business logic and routes can
be exercised without a real API key or network call to an LLM."""
from app.llm.base import LLMProvider, LLMResponse


class FakeLLMProvider(LLMProvider):
    def __init__(self, text: str):
        self._text = text
        self.calls: list[dict] = []

    def generate(self, prompt, *, system=None, temperature=0.2, max_output_tokens=2048, json_schema=None):
        self.calls.append({"prompt": prompt, "system": system, "json_schema": json_schema})
        return LLMResponse(text=self._text, model="fake", provider="fake")


class SequencedLLMProvider(LLMProvider):
    """Returns a different canned response on each successive call."""

    def __init__(self, texts: list[str]):
        self._texts = list(texts)
        self.calls: list[dict] = []

    def generate(self, prompt, *, system=None, temperature=0.2, max_output_tokens=2048, json_schema=None):
        self.calls.append({"prompt": prompt, "system": system, "json_schema": json_schema})
        text = self._texts.pop(0) if self._texts else "{}"
        return LLMResponse(text=text, model="fake", provider="fake")


class RoutedLLMProvider(LLMProvider):
    """Answers by which prompt is asking: the first `routes` key found in the
    system prompt picks the response. Robust to optional calls (e.g. a
    preference check that only runs when preferences exist)."""

    def __init__(self, routes: dict[str, str], default: str = "{}"):
        self._routes = routes
        self._default = default
        self.calls: list[dict] = []

    def generate(self, prompt, *, system=None, temperature=0.2, max_output_tokens=2048, json_schema=None):
        self.calls.append({"prompt": prompt, "system": system, "json_schema": json_schema})
        for key, text in self._routes.items():
            if key in (system or ""):
                return LLMResponse(text=text, model="fake", provider="fake")
        return LLMResponse(text=self._default, model="fake", provider="fake")

    def calls_for(self, key: str) -> list[dict]:
        return [c for c in self.calls if key in (c["system"] or "")]
