"""Default active provider (section 8): Gemini via Google AI Studio, using the
current `google-genai` SDK (the older `google-generativeai` package is deprecated)."""
import time

from app.llm.base import LLMProvider, LLMResponse

MAX_RETRIES = 4
RETRY_BASE_DELAY_SECONDS = 3.0


def _is_rate_limit_error(exc: Exception) -> bool:
    # google-genai's rate-limit exception lives under a private `_gaos` module
    # path that can move between SDK versions, so check the HTTP status
    # instead of importing that class directly.
    return getattr(exc, "status_code", None) == 429


class GeminiProvider(LLMProvider):
    def __init__(self, api_key: str, model: str):
        if not api_key:
            raise ValueError("GEMINI_API_KEY is not set - required when LLM_PROVIDER=gemini")
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.2,
        max_output_tokens: int = 2048,
    ) -> LLMResponse:
        for attempt in range(MAX_RETRIES):
            try:
                interaction = self._client.interactions.create(
                    model=self._model,
                    input=prompt,
                    system_instruction=system,
                    generation_config={
                        "temperature": temperature,
                        "max_output_tokens": max_output_tokens,
                    },
                )
                return LLMResponse(
                    text=interaction.output_text,
                    model=self._model,
                    provider="gemini",
                    raw=None,
                )
            except Exception as exc:
                is_last_attempt = attempt == MAX_RETRIES - 1
                if not _is_rate_limit_error(exc) or is_last_attempt:
                    raise
                time.sleep(RETRY_BASE_DELAY_SECONDS * (attempt + 1))

        raise RuntimeError("unreachable")  # loop always returns or raises
