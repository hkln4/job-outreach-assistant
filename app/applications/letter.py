"""Writing the letter (roadmap phase 4, item 2): the prose a company reads is
generated as plain text - "Subject: ..." on the first line, the body after it -
not squeezed inside JSON, where models write stiffer text and escape errors
cut letters short. The metadata (sources, evidence used, CV choice,
reasoning) comes from a small structured follow-up call over the finished
letter.

A writer that answers with the old JSON shape anyway is still understood (and
then its metadata is used without the follow-up call).
"""
import re

from pydantic import BaseModel, Field, field_validator

from app.applications.phrases import prompt_list
from app.llm.base import LLMProvider
from app.llm.call_log import llm_context
from app.llm.json_utils import parse_json_object
from app.llm.untrusted import UNTRUSTED_RULE

WRITE_SYSTEM_PROMPT = """You are writing a job application email on behalf of the owner (a new \
graduate), from his profile, his most relevant background records and the company's own text.

If a CHOSEN ANGLE is given, build the email around it: its company hook is the reason for \
writing, its evidence record is the lead project, its bridge is the link between them.

The company: say plainly what it builds and for whom, using only the company text. If a JOB \
POSTING BEING ANSWERED is given, the email answers that posting. If OWNER'S REASON FOR \
APPLYING is given, it is an open application: never imply a posting exists unless the text \
says so.

The owner: lead with ONE project or experience that genuinely fits (same problem, domain or \
technology used for the same purpose), in at most two short sentences - what he worked on, \
what he built, optionally one concrete result from the records. At most one more supporting \
sentence, only if it adds relevance. Never list unrelated projects to show breadth. Mention a \
technology only when a record shows he used it. Numbers only from the records. In PROFILE \
EVIDENCE, state facts only from FACTS lines, verbatim CV bullets and OWNER STATED lines; MODEL \
FRAMING lines may guide emphasis but are not a source. RESEARCH NOTES and the company pages are \
the only sources about the company.

Without a template, four short paragraphs: who he is and the position; one factual reason for \
contacting this company; the lead project (+ optional supporting sentence); a short closing \
that mentions the attached CV and asks for a conversation. With OWNER'S COVER LETTER TEMPLATE, \
follow its tone, greeting and closing, replacing its specific content.

Requirements:
- 130-180 words in Turkish, 100-150 in English. An introduction, not a CV summary.
- Language: OWNER'S PREFERRED EMAIL LANGUAGE when given; else JOB POSTING LANGUAGE; else the \
template's language; else Turkish. Never mix languages.
- Subject: "Junior <position> Başvurusu" (Turkish) or "Junior <position> Application" \
(English); no second "Junior" if the position has one.
- Write like a new graduate writing to a person. Do not praise the company or claim interest \
the owner's notes don't support. Show what he built instead of saying he is "strong".
- Never use these phrases (or close variants):
{banned}
- Do NOT write a signature, name, phone or links at the end - the app adds the signature block.
- Follow RECIPIENT and STANDING STYLE RULES when given. STYLE EXAMPLES show the owner's \
preferred voice - match their tone, never copy their content.

Output plain text only, exactly this shape:
Subject: <subject line>

<email body>
""" + UNTRUSTED_RULE

META_SYSTEM_PROMPT = """You are annotating a finished job application email. Do not change it.
Return:
- "target_position": the position the email applies for.
- "cv_choice_name": the CV variant (exact name from AVAILABLE CV VARIANTS) that fits it best.
- "sources": the [url] headers of the company pages whose facts the email uses (exact URLs).
- "evidence_item_ids": the E-numbers of the PROFILE EVIDENCE records the email draws on.
- "unsupported_claims": claims about the owner in the email that no record supports (normally empty).
- "reasoning": a short note to the owner in the email's language: what the company does, which \
project was used and why, why this position and CV.
- "claims": EVERY sentence of the email body, in order, each as:
  - "sentence": copied exactly from the body;
  - "kind": "company" (states something about the company), "owner" (states something about \
the owner - experience, project, skill, result), "both", or "none" (greeting, closing, request, \
mention of the attached CV - nothing to verify);
  - "supports": for company/owner/both, one entry per fact: "source" (a company [url] header \
copied exactly, or a record E-number like "E12") and "quote" (the words in THAT source that \
state the fact, copied exactly). Never use the email itself as a source. A fact you cannot \
find in a source gets no support - do not invent one.
""" + UNTRUSTED_RULE

SUBJECT_RE = re.compile(r"^\s*(?:subject|konu)\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


class _Support(BaseModel):
    source: str
    quote: str


class _Claim(BaseModel):
    sentence: str
    kind: str
    supports: list[_Support] = []


class LetterMetadata(BaseModel):
    claims: list[_Claim] = Field(default_factory=list)
    target_position: str = ""
    cv_choice_name: str = ""
    reasoning: str = ""
    sources: list[str] = []
    evidence_item_ids: list[int] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)

    @field_validator("evidence_item_ids", mode="before")
    @classmethod
    def _ids_from_labels(cls, value: object) -> object:
        if isinstance(value, list):
            return [str(v).strip().lstrip("Ee") if isinstance(v, str) else v for v in value]
        return value


def parse_letter(text: str) -> dict:
    """{subject, body, metadata?}. Accepts the plain-text shape, and the old
    JSON shape a writer may still return."""
    stripped = (text or "").strip()
    if stripped.startswith("{") or stripped.startswith("```"):
        data = parse_json_object(stripped, fallback={})
        if "parse_error" not in data:
            if not (data.get("cover_letter_body") or "").strip() or not (data.get("subject") or "").strip():
                raise ValueError("The writer returned an incomplete draft (no subject or body) - try again.")
            meta = {k: data[k] for k in LetterMetadata.model_fields if k in data}
            try:
                meta = LetterMetadata.model_validate(meta).model_dump() if meta else None
            except ValueError as exc:
                raise ValueError(f"The writer returned an incomplete draft (bad fields: {exc.errors()[0]['loc'][0]}) - try again.") from exc
            return {"subject": data["subject"].strip(), "body": data["cover_letter_body"].strip(), "metadata": meta}
    match = SUBJECT_RE.search(stripped)
    if match and stripped[: match.start()].strip() == "":
        subject = match.group(1).strip()
        body = stripped[match.end() :].strip()
    else:
        subject, body = "", stripped
    if not body:
        raise ValueError("The letter came back empty - try again.")
    if not subject:
        raise ValueError("The letter came back without a subject line - try again.")
    return {"subject": subject, "body": body, "metadata": None}


@llm_context("draft_write")
def write_letter(llm: LLMProvider, prompt: str) -> dict:
    response = llm.generate(
        prompt=prompt,
        system=WRITE_SYSTEM_PROMPT.replace("{banned}", prompt_list()),
        temperature=0.3,
        max_output_tokens=2000,
    )
    return parse_letter(response.text)


@llm_context("draft_metadata")
def letter_metadata(llm: LLMProvider, *, subject: str, body: str, context: str) -> dict:
    response = llm.generate(
        prompt=f"EMAIL SUBJECT: {subject}\n\nEMAIL BODY:\n{body}\n\n{context}",
        system=META_SYSTEM_PROMPT,
        temperature=0.0,
        max_output_tokens=3000,
        json_schema=LetterMetadata.model_json_schema(),
    )
    data = parse_json_object(response.text, fallback={})
    if "parse_error" in data:
        return LetterMetadata().model_dump() | {"metadata_error": "unreadable answer"}
    try:
        return LetterMetadata.model_validate(data).model_dump()
    except ValueError:
        return LetterMetadata().model_dump() | {"metadata_error": "wrong shape"}
