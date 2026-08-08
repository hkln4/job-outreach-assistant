"""Default active provider (section 8): Gemini via Google AI Studio."""
from app.llm.base import LLMProvider, LLMResponse


class GeminiProvider(LLMProvider):
    def __init__(self, api_key: str, model: str):
        if not api_key:
            raise ValueError("GEMINI_API_KEY is not set - required when LLM_PROVIDER=gemini")
        import google.generativeai as genai

        genai.configure(api_key=api_key)
        self._model_name = model
        self._genai = genai

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.2,
        max_output_tokens: int = 2048,
    ) -> LLMResponse:
        model = self._genai.GenerativeModel(
            self._model_name,
            system_instruction=system,
        )
        result = model.generate_content(
            prompt,
            generation_config=self._genai.types.GenerationConfig(
                temperature=temperature,
                max_output_tokens=max_output_tokens,
            ),
        )
        return LLMResponse(
            text=result.text,
            model=self._model_name,
            provider="gemini",
            raw=None,
        )
