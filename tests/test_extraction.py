from app.profile_builder.extraction import safe_extract_item


class _FailingProvider:
    def generate(self, prompt, *, system=None, temperature=0.2, max_output_tokens=2048):
        raise RuntimeError("Your credit balance is too low to access the Anthropic API.")


def test_safe_extract_item_survives_llm_failure():
    result = safe_extract_item(_FailingProvider(), "some CV text")
    assert result["purpose"] == "unknown"
    assert "parse_error" in result
    assert "credit balance" in result["parse_error"]
