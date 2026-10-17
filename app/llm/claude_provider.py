"""
Claude provider (section 8): implemented but inactive until ANTHROPIC_API_KEY is set
and LLM_PROVIDER=claude. No other code changes needed to switch over.
"""
from app.llm.base import LLMProvider, LLMResponse

# Models that still take temperature and run without thinking unless asked.
# Everything newer (Sonnet 5.5, Haiku 5.5, Opus 4.7+, ...) rejects a
# non-default temperature with a 400 and thinks adaptively by default.
_LEGACY_MODEL_PREFIXES = ("claude-3", "claude-haiku-4", "claude-sonnet-4", "claude-opus-4-5", "claude-opus-4-6")
# Thinking tokens count against max_tokens on the newer models - leave room so
# the JSON answer isn't cut off behind them.
THINKING_HEADROOM_TOKENS = 8000
# Models that accept the server-side refusal fallback ("default" form): a
# safety-classifier decline is retried on a fallback model inside the same call.
_FALLBACK_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeProvider(LLMProvider):
    def __init__(self, api_key: str, model: str):
        if not api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is not set - required when LLM_PROVIDER=claude. "
                "Leave LLM_PROVIDER=gemini until this is funded."
            )
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.2,
        max_output_tokens: int = 2048,
    ) -> LLMResponse:
        request = {
            "model": self._model,
            "system": system or "",
            "messages": [{"role": "user", "content": prompt}],
        }
        if self._model.startswith(_LEGACY_MODEL_PREFIXES):
            message = self._client.messages.create(
                **request, max_tokens=max_output_tokens, temperature=temperature
            )
        else:
            request.update(
                max_tokens=max_output_tokens + THINKING_HEADROOM_TOKENS,
                output_config={"effort": "medium"},
            )
            if self._model.startswith(_FALLBACK_MODELS):
                message = self._client.beta.messages.create(
                    **request, betas=[FALLBACK_BETA], fallbacks="default"
                )
            else:
                message = self._client.messages.create(**request)

        if message.stop_reason == "refusal":
            raise ValueError(f"{self._model} declined this request - try again or rephrase the input.")
        text = "".join(block.text for block in message.content if block.type == "text")
        return LLMResponse(
            text=text,
            model=getattr(message, "model", self._model),
            provider="claude",
            raw=None,
        )
