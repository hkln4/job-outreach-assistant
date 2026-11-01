from types import SimpleNamespace

from app.llm.gemini_provider import GeminiProvider


class _RateLimitError(Exception):
    status_code = 429


def test_generate_retries_on_rate_limit_then_succeeds(monkeypatch):
    provider = GeminiProvider(api_key="dummy-key-not-real", model="gemini-3.6-flash")
    monkeypatch.setattr("app.llm.gemini_provider.time.sleep", lambda _: None)  # don't actually wait in tests

    calls = {"count": 0}

    def fake_create(**kwargs):
        calls["count"] += 1
        if calls["count"] < 3:
            raise _RateLimitError("quota exceeded, retry in 2s")
        return SimpleNamespace(output_text="ok after retries")

    provider._client.interactions.create = fake_create

    result = provider.generate("hello")
    assert result.text == "ok after retries"
    assert calls["count"] == 3


def test_generate_gives_up_after_max_retries(monkeypatch):
    provider = GeminiProvider(api_key="dummy-key-not-real", model="gemini-3.6-flash")
    monkeypatch.setattr("app.llm.gemini_provider.time.sleep", lambda _: None)

    def always_rate_limited(**kwargs):
        raise _RateLimitError("still over quota")

    provider._client.interactions.create = always_rate_limited

    try:
        provider.generate("hello")
        assert False, "expected the rate limit error to propagate after exhausting retries"
    except _RateLimitError:
        pass


def test_generate_does_not_retry_non_rate_limit_errors(monkeypatch):
    provider = GeminiProvider(api_key="dummy-key-not-real", model="gemini-3.6-flash")
    monkeypatch.setattr("app.llm.gemini_provider.time.sleep", lambda _: (_ for _ in ()).throw(AssertionError("should not sleep")))

    calls = {"count": 0}

    def broken(**kwargs):
        calls["count"] += 1
        raise ValueError("some other unrelated error")

    provider._client.interactions.create = broken

    try:
        provider.generate("hello")
        assert False, "expected ValueError to propagate immediately"
    except ValueError:
        pass
    assert calls["count"] == 1


def test_schema_requests_json_output():
    provider = GeminiProvider(api_key="dummy-key-not-real", model="gemini-3.6-flash")
    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(output_text="{}")

    provider._client.interactions.create = fake_create
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}

    provider.generate("hello")
    provider.generate("hello", json_schema=schema)

    assert "response_format" not in calls[0]
    assert calls[1]["response_mime_type"] == "application/json"
    assert calls[1]["response_format"] == {"type": "text", "mime_type": "application/json", "schema": schema}


def test_temperature_is_not_sent_and_zero_means_a_fixed_seed():
    """The Interactions API has no temperature field - the SDK used to drop it
    silently. Deterministic calls (temperature 0) get a fixed seed instead."""
    provider = GeminiProvider(api_key="dummy-key-not-real", model="gemini-3.6-flash")
    calls = []
    provider._client.interactions.create = lambda **kwargs: calls.append(kwargs) or SimpleNamespace(output_text="{}")

    provider.generate("extract", temperature=0.0, max_output_tokens=500)
    provider.generate("write", temperature=0.3, max_output_tokens=900)

    assert calls[0]["generation_config"] == {"max_output_tokens": 500, "seed": 0}
    assert calls[1]["generation_config"] == {"max_output_tokens": 900}
