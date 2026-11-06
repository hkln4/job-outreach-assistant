"""
Claude provider (section 8), selected with LLM_PROVIDER=claude. Reading tasks use
ANTHROPIC_MODEL; letters use ANTHROPIC_DRAFT_MODEL (see app/llm/factory.py).
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
                "Set it in .env, or switch to LLM_PROVIDER=gemini."
            )
        import anthropic

        self._transform_schema = anthropic.transform_schema
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.2,
        max_output_tokens: int = 2048,
        json_schema: dict | None = None,
    ) -> LLMResponse:
        request = {
            "model": self._model,
            "system": system or "",
            "messages": [{"role": "user", "content": prompt}],
        }
        output_config = {}
        if json_schema is not None:
            # Structured outputs: the answer is constrained to the schema. The
            # SDK helper rewrites a Pydantic schema into the strict subset the
            # API accepts (additionalProperties: false on every object,
            # unsupported keywords moved into descriptions).
            output_config["format"] = {"type": "json_schema", "schema": self._transform_schema(json_schema)}
        if self._model.startswith(_LEGACY_MODEL_PREFIXES):
            if output_config:
                request["output_config"] = output_config
            message = self._client.messages.create(
                **request, max_tokens=max_output_tokens, temperature=temperature
            )
        else:
            request.update(
                max_tokens=max_output_tokens + THINKING_HEADROOM_TOKENS,
                output_config={"effort": "medium", **output_config},
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
        usage = getattr(message, "usage", None)
        return LLMResponse(
            text=text,
            model=getattr(message, "model", self._model),
            provider="claude",
            raw=None,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        )
