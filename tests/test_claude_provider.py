"""Request shape per model generation: older models take temperature; newer
ones 400 on it, think adaptively (so they need max_tokens headroom), and get
the server-side refusal fallback where supported."""
from types import SimpleNamespace

import pytest

from app.llm.claude_provider import FALLBACK_BETA, THINKING_HEADROOM_TOKENS, ClaudeProvider


class _Recorder:
    def __init__(self, stop_reason="end_turn"):
        self.calls = []
        self._stop_reason = stop_reason

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            stop_reason=self._stop_reason,
            model=kwargs["model"],
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text='{"ok": 1}')],
        )


def _provider(model, stop_reason="end_turn"):
    provider = ClaudeProvider(api_key="test-key-not-real", model=model)
    plain, beta = _Recorder(stop_reason), _Recorder(stop_reason)
    provider._client = SimpleNamespace(messages=plain, beta=SimpleNamespace(messages=beta))
    return provider, plain, beta


def test_legacy_model_keeps_temperature_and_token_cap():
    provider, plain, beta = _provider("claude-haiku-4-5")
    response = provider.generate("hi", system="s", temperature=0.3, max_output_tokens=1000)
    (call,) = plain.calls
    assert call["temperature"] == 0.3 and call["max_tokens"] == 1000
    assert beta.calls == []
    assert response.text == '{"ok": 1}'


def test_sonnet_5_5_drops_temperature_adds_headroom_and_fallback():
    provider, plain, beta = _provider("claude-sonnet-5-5")
    response = provider.generate("hi", temperature=0.3, max_output_tokens=4096)
    (call,) = beta.calls
    assert "temperature" not in call
    assert call["max_tokens"] == 4096 + THINKING_HEADROOM_TOKENS
    assert call["output_config"] == {"effort": "medium"}
    assert call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
    assert plain.calls == []
    assert response.text == '{"ok": 1}'  # thinking blocks are skipped


def test_refusal_is_a_readable_error():
    provider, _, _ = _provider("claude-sonnet-5-5", stop_reason="refusal")
    with pytest.raises(ValueError, match="declined"):
        provider.generate("hi")
