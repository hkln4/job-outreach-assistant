"""Preference conflict checker (CLAUDE.md section 4): evidence-first - the LLM
flags conflicts against the owner's hard/soft preferences using only the extracted
company text, never the company's name or reputation. A flag for the owner's
judgment, never an automatic verdict."""
from app.llm.base import LLMProvider
from app.llm.json_utils import parse_json_object
from app.llm.untrusted import UNTRUSTED_RULE, wrap_untrusted
from app.models import Preference

CHECK_SYSTEM_PROMPT = """You are checking whether a company's website text conflicts with \
any of the owner's stated preferences/constraints.

Rules:
- Base every flag ONLY on text explicitly present in the given company text - never guess \
from the company name or general reputation. Website text can mislead either way.
- For each preference that appears to match, quote the exact on-site snippet that triggered it.
- If nothing in the text supports a match, do not flag that preference.
""" + UNTRUSTED_RULE + """\
- Output ONLY valid JSON, this exact shape:
{"flags": [{"preference_id": int, "evidence_snippet": string, "confidence": "low"|"medium"|"high"}]}
"""

CHECK_FALLBACK = {"flags": []}


def check_company(llm: LLMProvider, company_text: str, preferences: list[Preference]) -> list[dict]:
    if not preferences:
        return []

    pref_list = "\n".join(
        f"- id={p.id} [{p.type.value}] {p.description} (signals: {', '.join(p.signals) or 'none'})"
        for p in preferences
    )
    prompt = f"OWNER PREFERENCES:\n{pref_list}\n\nCOMPANY TEXT:\n{wrap_untrusted(company_text[:24000])}"

    response = llm.generate(prompt=prompt, system=CHECK_SYSTEM_PROMPT, temperature=0.0, max_output_tokens=1024)
    data = parse_json_object(response.text, fallback=CHECK_FALLBACK)
    return data.get("flags", [])
