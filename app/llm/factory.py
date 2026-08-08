"""Picks the active provider from Settings.llm_provider. This is the only place
that branches on provider name - everything else depends on LLMProvider."""
from functools import lru_cache

from app.config import Settings, get_settings
from app.llm.base import LLMProvider


@lru_cache
def get_llm_provider() -> LLMProvider:
    settings: Settings = get_settings()

    if settings.llm_provider == "gemini":
        from app.llm.gemini_provider import GeminiProvider

        return GeminiProvider(api_key=settings.gemini_api_key, model=settings.gemini_model)

    if settings.llm_provider == "claude":
        from app.llm.claude_provider import ClaudeProvider

        return ClaudeProvider(api_key=settings.anthropic_api_key, model=settings.anthropic_model)

    raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider!r} (expected 'gemini' or 'claude')")
