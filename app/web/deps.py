"""Shared FastAPI dependencies. get_llm is a thin indirection so tests can override
it with a fake provider via app.dependency_overrides instead of needing a real API key."""
from app.llm.base import LLMProvider
from app.llm.factory import get_llm_provider


def get_llm() -> LLMProvider:
    return get_llm_provider()
