"""Independent reviewer pass on a draft: a second, cheaper model call that did
not write the letter reads it next to the same company text, profile evidence
and template, and lists what to fix. It never rewrites the draft and never
blocks a send - its notes join the deterministic checks in the review page's
"check before sending" box, and the owner can send them back through the
normal reject/regenerate path.

The app decides what counts (same principle as the Evidence Rule): an issue
whose quote isn't literally in the draft is dropped, so the reviewer can't
point at text that doesn't exist.
"""
import logging
import re
from typing import Literal

from pydantic import BaseModel, ValidationError, field_validator

from app.applications.evidence import evidence_prompt_section
from app.llm.base import LLMProvider
from app.llm.json_utils import parse_json_object
from app.llm.untrusted import UNTRUSTED_RULE, wrap_untrusted
from app.models import Company, ProfileItem

logger = logging.getLogger("app.draft_review")

MAX_ISSUES = 8
MIN_QUOTE_CHARS = 3
ISSUE_KINDS = (
    "unsupported_claim",
    "generic_phrase",
    "wrong_language",
    "off_length",
    "weak_company_reason",
    "irrelevant_project",
    "other",
)

REVIEW_SYSTEM_PROMPT = """You are reviewing a job application email that someone else \
drafted for the owner (a new graduate). Do not rewrite it - list what the owner should \
fix before sending.

Check the draft against the COMPANY TEXT, the PROFILE EVIDENCE records and, if given, \
the OWNER'S COVER LETTER TEMPLATE:
- unsupported_claim: a statement about the company that the company text doesn't \
support, or a claim about the owner (skill, project, result, experience) that the \
evidence records don't support.
- generic_phrase: stock cover-letter wording that could go to any company.
- wrong_language: mixed languages, or a language other than OWNER'S PREFERRED EMAIL \
LANGUAGE (when given), else other than the template's language (when one is given).
- off_length: clearly outside 100-180 words.
- weak_company_reason: the reason for contacting this company is vague, flattering, \
or not grounded in what the company text says it does.
- irrelevant_project: a project or skill mentioned that has nothing to do with this \
company or position.
- other: anything else that would embarrass the owner if sent.

Rules:
- "quote" must be copied EXACTLY, character for character, from the draft's subject or \
body - a short span (a phrase or one sentence) that shows the problem. Never paraphrase.
- "suggestion" says concretely what to change, in English, in one sentence.
- Report at most 8 issues, most important first. Report none if the draft is fine.
- "overall": one sentence on whether the draft is ready to send.
""" + UNTRUSTED_RULE + """\
Output ONLY valid JSON, this exact shape:
{"issues": [{"kind": string, "quote": string, "suggestion": string}], "overall": string}
"""


class ReviewIssue(BaseModel):
    kind: Literal[ISSUE_KINDS]
    quote: str
    suggestion: str

    @field_validator("kind", mode="before")
    @classmethod
    def _unknown_kind_is_other(cls, value: object) -> object:
        return value if value in ISSUE_KINDS else "other"


class ReviewAnswer(BaseModel):
    issues: list[ReviewIssue] = []
    overall: str = ""


def _normalize(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip().casefold()


def quote_in_draft(quote: str, subject: str, body: str) -> bool:
    needle = _normalize(quote).strip("\"'")
    return len(needle) >= MIN_QUOTE_CHARS and needle in _normalize(f"{subject}\n{body}")


def review_draft(
    llm: LLMProvider,
    *,
    company: Company,
    subject: str,
    body: str,
    evidence_items: list[ProfileItem],
    template: str = "",
    language: str = "",
) -> dict:
    """{"issues": [...], "overall": str, "dropped_quotes": int}, or {"error": str}
    when the call or its answer failed - a failed review must never stop a draft."""
    pages = (company.research or {}).get("pages", [])
    company_text = "\n\n---\n\n".join(f"[{p['url']}]\n{p['text']}" for p in pages)
    prompt = (
        f"DRAFT SUBJECT:\n{subject}\n\nDRAFT BODY:\n{body}\n\n"
        f"PROFILE EVIDENCE:\n{evidence_prompt_section(evidence_items) or 'none'}\n\n"
        f"COMPANY: {company.name}\n\nCOMPANY TEXT:\n{wrap_untrusted(company_text[:16000])}"
    )
    if language:
        prompt += f"\n\nOWNER'S PREFERRED EMAIL LANGUAGE:\n{language}"
    if template.strip():
        prompt += f"\n\nOWNER'S COVER LETTER TEMPLATE:\n{template.strip()}"

    try:
        response = llm.generate(
            prompt=prompt,
            system=REVIEW_SYSTEM_PROMPT,
            temperature=0.0,
            max_output_tokens=1200,
            json_schema=ReviewAnswer.model_json_schema(),
        )
    except Exception as exc:  # noqa: BLE001 - shown on the review page, never blocks the draft
        logger.warning("Draft review call failed: %s", exc)
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}

    data = parse_json_object(response.text, fallback={})
    if "parse_error" in data:
        return {"error": "the reviewer's answer wasn't valid JSON"}
    try:
        answer = ReviewAnswer.model_validate(data)
    except ValidationError as exc:
        return {"error": f"the reviewer's answer had the wrong shape ({exc.error_count()} errors)"}

    kept = [issue for issue in answer.issues if quote_in_draft(issue.quote, subject, body)]
    return {
        "issues": [issue.model_dump() for issue in kept[:MAX_ISSUES]],
        "overall": answer.overall.strip(),
        "dropped_quotes": len(answer.issues) - len(kept),
    }


def review_feedback(review: dict | None) -> str:
    """The reviewer's issues as reject feedback for the regenerate path."""
    issues = (review or {}).get("issues") or []
    if not issues:
        return ""
    lines = ["Fix these points from the review, keep everything else:"]
    lines += [f'- "{issue["quote"]}": {issue["suggestion"]}' for issue in issues]
    return "\n".join(lines)
