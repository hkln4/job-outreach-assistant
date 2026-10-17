"""Shared FastAPI dependencies. get_llm / get_draft_llm are thin indirections so
tests can override them with a fake provider via app.dependency_overrides
instead of needing a real API key."""
from app.llm.base import LLMProvider
from app.llm.factory import get_draft_llm_provider, get_llm_provider


def get_llm() -> LLMProvider:
    return get_llm_provider()


def get_draft_llm() -> LLMProvider:
    """For routes that write a letter (see Settings.anthropic_draft_model)."""
    return get_draft_llm_provider()
