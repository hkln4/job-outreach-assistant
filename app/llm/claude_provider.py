"""
Claude provider (section 8): implemented but inactive until ANTHROPIC_API_KEY is set
and LLM_PROVIDER=claude. No other code changes needed to switch over.
"""
from app.llm.base import LLMProvider, LLMResponse


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
        message = self._client.messages.create(
            model=self._model,
            max_tokens=max_output_tokens,
            temperature=temperature,
            system=system or "",
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in message.content if block.type == "text")
        return LLMResponse(
            text=text,
            model=self._model,
            provider="claude",
            raw=None,
        )
