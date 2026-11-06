"""The angle step (roadmap phase 4, item 1): before writing, the draft model
proposes 2-3 angles - a company hook quoted exactly from one of its pages, the
owner's evidence record that answers it, and one sentence bridging the two.

The app checks each angle (the Evidence Rule, enforced by code): the hook
quote must be on a fetched page (its URL is corrected to the page it is
really on), and the evidence id must be one that was offered. Angles that fail
are dropped and counted. The owner picks one on the review page (default: the
first); "regenerate with another angle" reuses the saved angles - no new call.
"""
from pydantic import BaseModel

from app.companies.context import locate_quote
from app.llm.base import LLMProvider
from app.llm.call_log import llm_context
from app.llm.json_utils import parse_json_object
from app.llm.untrusted import UNTRUSTED_RULE, wrap_untrusted

MAX_ANGLES = 3

ANGLE_SYSTEM_PROMPT = """You are planning a job application email for the owner (a new graduate). \
Do NOT write the email. Propose 2-3 different ANGLES - each a reason this owner writes to this \
company, built from one concrete company fact and one concrete owner record.

For each angle:
- "company_hook": a short span copied EXACTLY from the COMPANY TEXT (character for character) \
that states something specific the company does or needs.
- "hook_url": the [url] header of the page that span is on.
- "owner_evidence_id": the E-number of ONE record from PROFILE EVIDENCE that genuinely matches \
the hook (same problem, domain or technology used for the same purpose - not just a shared \
language or framework).
- "bridge": one plain sentence linking the two, true for both sides.
- "position": the role this angle applies for.
Order them strongest first. Prefer angles a reader at this company would recognise as specific \
to them.
""" + UNTRUSTED_RULE


class _Angle(BaseModel):
    company_hook: str
    hook_url: str
    owner_evidence_id: int
    bridge: str
    position: str


class AnglesAnswer(BaseModel):
    angles: list[_Angle]


def verify_angles(raw: list[dict], pages: list[dict], offered_ids: set[int]) -> tuple[list[dict], int]:
    """(kept angles, how many were dropped)."""
    kept, dropped = [], 0
    for angle in raw:
        page_url = locate_quote(angle.get("company_hook", ""), pages)
        if page_url is None or angle.get("owner_evidence_id") not in offered_ids:
            dropped += 1
            continue
        kept.append({**angle, "hook_url": page_url})
    return kept[:MAX_ANGLES], dropped


@llm_context("draft_angles")
def propose_angles(llm: LLMProvider, *, company_name: str, company_text: str, evidence_section: str, owner_context: str) -> list[dict]:
    """Raw angles from the model ([] on any failure - drafting goes on without one)."""
    prompt = (
        f"{owner_context}\n\nPROFILE EVIDENCE:\n{evidence_section or 'none'}\n\n"
        f"COMPANY: {company_name}\n\nCOMPANY TEXT:\n{wrap_untrusted(company_text)}"
    )
    try:
        response = llm.generate(
            prompt=prompt,
            system=ANGLE_SYSTEM_PROMPT,
            temperature=0.2,
            max_output_tokens=1200,
            json_schema=AnglesAnswer.model_json_schema(),
        )
    except Exception:  # noqa: BLE001 - an angle is a help, never a requirement
        return []
    data = parse_json_object(response.text, fallback={})
    try:
        return AnglesAnswer.model_validate(data).model_dump()["angles"]
    except ValueError:
        return []
