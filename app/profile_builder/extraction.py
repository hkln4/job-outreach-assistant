"""Per-item structured extraction (CLAUDE.md section 3: "per-item deep pass").
One LLM call per item, forced structured JSON output. Never invents - unknown
fields must be explicit, never hand-waved."""
from app.llm.base import LLMProvider
from app.llm.json_utils import parse_json_object

EXTRACTION_SYSTEM_PROMPT = """You are extracting a structured record from ONE piece of \
evidence about a person's background (a CV, a GitHub repo, a LinkedIn export, or a \
personal site page).

Rules:
- Only use what is explicitly present in the given text. Never invent skills, tech, or \
achievements.
- If a field cannot be determined from the text, set it to the literal string "unknown" - \
never guess.
- Output ONLY valid JSON, no prose, no markdown fences, matching exactly this shape:
{"purpose": string, "stack": [string], "role": string, "highlights": [string]}
"""

EXTRACTION_FALLBACK = {"purpose": "unknown", "stack": [], "role": "unknown", "highlights": []}


def extract_item(llm: LLMProvider, raw_text: str) -> dict:
    response = llm.generate(
        prompt=raw_text[:12000],  # cost discipline (section 8): trimmed input
        system=EXTRACTION_SYSTEM_PROMPT,
        temperature=0.0,
        max_output_tokens=1024,
    )
    return parse_json_object(response.text, fallback=EXTRACTION_FALLBACK)


def safe_extract_item(llm: LLMProvider, raw_text: str) -> dict:
    """Same as extract_item, but an LLM failure (rate limit, no credit, auth,
    network) becomes a needs_review record instead of propagating - so one bad
    item never crashes a batch or the request. Errors the owner should actually
    see (500 with a clear message) still surface for single-item, single-shot
    actions via app/web/errors.py; this is specifically for the ingestion loops
    where "keep going" is the more useful behavior."""
    try:
        return extract_item(llm, raw_text)
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
        return {**EXTRACTION_FALLBACK, "parse_error": str(exc)}
