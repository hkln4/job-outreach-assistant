"""Picks the active provider from Settings.llm_provider. This is the only place
that branches on provider name - everything else depends on LLMProvider."""
from functools import lru_cache

from app.config import Settings, get_settings
from app.llm.base import LLMProvider


def _build(settings: Settings, *, draft: bool) -> LLMProvider:
    if settings.llm_provider == "gemini":
        from app.llm.gemini_provider import GeminiProvider

        model = (draft and settings.gemini_draft_model) or settings.gemini_model
        return GeminiProvider(api_key=settings.gemini_api_key, model=model)

    if settings.llm_provider == "claude":
        from app.llm.claude_provider import ClaudeProvider

        model = (draft and settings.anthropic_draft_model) or settings.anthropic_model
        return ClaudeProvider(api_key=settings.anthropic_api_key, model=model)

    raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider!r} (expected 'gemini' or 'claude')")


@lru_cache
def get_llm_provider() -> LLMProvider:
    """Research, extraction, preference checks - the reading work."""
    return _build(get_settings(), draft=False)


@lru_cache
def get_draft_llm_provider() -> LLMProvider:
    """Letters that go to a company: drafts, follow-ups, escalations."""
    return _build(get_settings(), draft=True)
