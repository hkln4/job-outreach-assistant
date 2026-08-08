"""Shared helper: LLM prompts in this app force structured JSON output. Centralized
here so every caller handles malformed output (fenced code blocks, stray prose) the
same way instead of trusting the model to always emit clean JSON."""
import json


def parse_json_object(text: str, fallback: dict) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {**fallback, "parse_error": text[:500]}
