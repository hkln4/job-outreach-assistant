import pytest

from app.config import get_settings
from app.llm.factory import get_llm_provider


def test_claude_provider_inactive_without_key(monkeypatch):
    """Section 8: Claude is implemented but inactive until ANTHROPIC_API_KEY is set."""
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    get_llm_provider.cache_clear()

    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        get_llm_provider()

    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    get_settings.cache_clear()
    get_llm_provider.cache_clear()


def test_unknown_provider_rejected(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "not-a-real-provider")
    get_settings.cache_clear()
    get_llm_provider.cache_clear()

    with pytest.raises(ValueError, match="Unknown LLM_PROVIDER"):
        get_llm_provider()

    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    get_settings.cache_clear()
    get_llm_provider.cache_clear()


def test_gemini_provider_constructs_with_key(monkeypatch):
    """Section 8: Gemini is the active default - client construction must not
    require a network call (no real API key available in tests)."""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    get_settings.cache_clear()
    get_llm_provider.cache_clear()

    provider = get_llm_provider()
    assert provider.__class__.__name__ == "GeminiProvider"

    get_settings.cache_clear()
    get_llm_provider.cache_clear()
