"""Homepage screening for directory candidates, so a list of ~400 legal names
becomes something the owner can actually review: what each company does, how
well it fits the owner's target roles, and whether it trips a preference
(e.g. defence) - with the on-site snippet that triggered it.

Deliberately much lighter than full research (app/companies/workflow.py):
one homepage, one small call. Full research runs later, only for companies
the owner approves.
"""
from datetime import datetime, timezone
from typing import Literal

import httpx
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.companies.fetch import (
    BROWSER_HEADERS,
    MIN_USABLE_RESEARCH_CHARS,
    _extract_main_text,
    _find_careers_link,
    _js_redirect_candidates,
    _trim_keep_head_and_tail,
)
from app.llm.base import LLMProvider
from app.llm.json_utils import parse_json_object
from app.llm.untrusted import UNTRUSTED_RULE, wrap_untrusted
from app.models import CompanyCandidate, Preference, Profile

MAX_HOMEPAGE_CHARS = 4000

TRIAGE_SYSTEM_PROMPT = """You are screening a company for a job seeker, from its homepage \
text only. Base everything on the given text - never on the company's name or reputation.

Return:
- "brand_name": the short name the company uses for itself on the site (e.g. "BeeVision" \
rather than the legal "3DTİM ELEKTRONİK A.Ş."). null if unclear.
- "summary": one plain English sentence on what the company actually does - no buzzwords.
- "sector": a few words, e.g. "computer vision software", "defence electronics".
- "fit": how well the company's work matches the OWNER TARGET ROLES - "high" (core \
software/ML/data work of that kind), "medium" (some such work, or unclear), "low" \
(mostly hardware, manufacturing, consulting, sales or unrelated).
- "fit_reason": one short sentence explaining the fit.
- "flags": for each OWNER PREFERENCE the homepage text appears to conflict with, \
{"preference_id": int, "evidence_snippet": the exact on-site words}. Empty if none. Only \
flag what the text supports.
Output ONLY valid JSON, this exact shape:
{"brand_name": string | null, "summary": string, "sector": string, \
"fit": "high" | "medium" | "low", "fit_reason": string, \
"flags": [{"preference_id": int, "evidence_snippet": string}]}
""" + UNTRUSTED_RULE


class _Flag(BaseModel):
    preference_id: int
    evidence_snippet: str = ""


class TriageResult(BaseModel):
    brand_name: str | None = None
    summary: str = ""
    sector: str = ""
    fit: Literal["high", "medium", "low"]  # required: an unreadable answer must never default to a fit
    fit_reason: str = ""
    flags: list[_Flag] = Field(default_factory=list)


def _fetch_homepage(url: str) -> tuple[str, str]:
    """(html, main text) - follows a JavaScript language splash once, like research does."""
    with httpx.Client(timeout=15, follow_redirects=True, headers=BROWSER_HEADERS) as client:
        resp = client.get(url)
        resp.raise_for_status()
        html, text = resp.text, _extract_main_text(resp.text)
        if len(text) < MIN_USABLE_RESEARCH_CHARS:
            for candidate in _js_redirect_candidates(html, url):
                try:
                    alt = client.get(candidate)
                    alt.raise_for_status()
                except httpx.HTTPError:
                    continue
                alt_text = _extract_main_text(alt.text)
                if len(alt_text) > len(text):
                    html, text = alt.text, alt_text
                    break
    return html, text


def _screen(llm: LLMProvider, prompt: str) -> TriageResult | None:
    """One retry on an unreadable answer (seen in practice: a comment written
    inside the JSON), then give up rather than guess."""
    for attempt in range(2):
        if attempt:
            prompt += "\n\nYour previous answer was not valid JSON. Output ONLY the JSON object - no comments."
        response = llm.generate(prompt=prompt, system=TRIAGE_SYSTEM_PROMPT, temperature=0.0, max_output_tokens=600)
        data = parse_json_object(response.text, fallback={})
        if "parse_error" in data:
            continue
        try:
            return TriageResult.model_validate(data)
        except ValidationError:
            continue
    return None


def _record(db: Session, candidate: CompanyCandidate, triage: dict) -> CompanyCandidate:
    candidate.triage = triage
    candidate.triaged_at = datetime.now(timezone.utc)
    db.commit()
    return candidate


def triage_candidate(db: Session, llm: LLMProvider, candidate: CompanyCandidate) -> CompanyCandidate:
    if not candidate.website_url:
        return _record(db, candidate, {"fit": "unknown", "error": "The directory lists no website."})
    try:
        html, text = _fetch_homepage(candidate.website_url)
    except Exception as exc:  # noqa: BLE001 - an unreachable site is a result, not a crash
        return _record(db, candidate, {"fit": "unknown", "error": f"Site couldn't be opened: {exc}"[:300]})
    if len(text) < MIN_USABLE_RESEARCH_CHARS:
        # Nothing to judge from - don't pay for a call that can only guess.
        return _record(db, candidate, {"fit": "unknown", "error": "The homepage has no readable text."})

    profile = db.query(Profile).first()
    preferences = db.query(Preference).all()
    roles = ", ".join(profile.target_roles) if profile else "unknown"
    pref_lines = "\n".join(f"- id={p.id} [{p.type.value}] {p.description}" for p in preferences) or "none"
    prompt = (
        f"OWNER TARGET ROLES: {roles}\n\nOWNER PREFERENCES:\n{pref_lines}\n\n"
        + (f"DIRECTORY CATEGORY: {wrap_untrusted(candidate.sector, label='DIRECTORY TEXT')}\n\n" if candidate.sector else "")
        + f"HOMEPAGE ({candidate.website_url}):\n"
        f"{wrap_untrusted(_trim_keep_head_and_tail(text, MAX_HOMEPAGE_CHARS))}"
    )
    result = _screen(llm, prompt)
    if result is None:
        return _record(db, candidate, {"fit": "unknown", "error": "Screening returned an unreadable answer."})

    by_id = {p.id: p for p in preferences}
    flags = [
        {
            "preference_id": flag.preference_id,
            "type": by_id[flag.preference_id].type.value,
            "description": by_id[flag.preference_id].description,
            "evidence_snippet": flag.evidence_snippet,
        }
        for flag in result.flags
        if flag.preference_id in by_id  # an invented id is dropped, not shown
    ]
    return _record(
        db,
        candidate,
        {
            "brand_name": (result.brand_name or "").strip() or None,
            "summary": result.summary,
            "sector": result.sector,
            "fit": result.fit,
            "fit_reason": result.fit_reason,
            "careers_page": _find_careers_link(html, candidate.website_url) is not None,
            "flags": flags,
        },
    )
