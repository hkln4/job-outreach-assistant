"""Fit evaluation and legitimacy flags for a posting (roadmap phase 3).

Gates (app/postings/gates.py) run first, by code. Only a posting that isn't
rejected by them gets the LLM fit call (reading model, evidence-quoted): a
0-100 score per dimension - technical, experience, domain, career alignment -
each with the posting quote and the profile record ids behind it, plus
strengths and gaps. Code then checks the work: a quote that isn't in the
posting is dropped, an evidence id that wasn't offered is dropped, and the
verdict is computed from the scores and gates by code. Nothing is
auto-dismissed - the verdict is shown, the owner decides.

Legitimacy flags never affect the score: no identifiable company, a posting
older than POSTING_STALE_DAYS, a repost of the same text, an off-domain apply
link.
"""
from datetime import datetime, timezone

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.applications.draft_review import quote_in_draft
from app.applications.evidence import _index_line, candidate_items, pre_rank
from app.channels.detect import classify_ats_url
from app.channels.domains import registrable_domain
from app.config import get_settings
from app.llm.base import LLMProvider
from app.llm.call_log import llm_context, set_llm_ids
from app.llm.json_utils import parse_json_object
from app.llm.untrusted import UNTRUSTED_RULE, wrap_untrusted
from app.models import JobPosting, Profile

DIMENSIONS = ("technical", "experience", "domain", "career")

FIT_SYSTEM_PROMPT = """You are judging how well a job posting fits the owner (a new graduate), \
from the POSTING text and the owner's PROFILE RECORDS only.

For each dimension give a 0-100 score, the posting words it is about ("posting_quote", copied \
EXACTLY from the posting) and the record ids that back the owner side ("evidence_ids", from \
the E-numbers listed):
- technical: required technologies/skills vs what the records show he used.
- experience: required experience/level vs a new graduate's records (be honest: "5+ years" \
is a low score, not a pass).
- domain: the posting's domain (e.g. medical imaging, fintech) vs his projects' domains.
- career: whether the role matches his TARGET ROLES.
Then "strengths" and "gaps": short points, each with a posting quote. A gap is something the \
posting asks for that no record shows - never pretend adjacent work is the same thing.
""" + UNTRUSTED_RULE


class _Dimension(BaseModel):
    score: int
    posting_quote: str
    evidence_ids: list[int]
    note: str


class _Point(BaseModel):
    point: str
    posting_quote: str


class _Scores(BaseModel):
    technical: _Dimension
    experience: _Dimension
    domain: _Dimension
    career: _Dimension


class FitAnswer(BaseModel):
    scores: _Scores
    strengths: list[_Point]
    gaps: list[_Point]


def _verified_quote(quote: str, posting: JobPosting) -> str:
    return quote if quote_in_draft(quote, posting.title, posting.description_text) else ""


def verdict_for(scores: dict, gates: dict) -> str:
    """strong | possible | weak | blocked - by code, from scores and gates."""
    if (gates.get("location") or {}).get("result") == "reject":
        return "blocked"
    values = [scores[d]["score"] for d in DIMENSIONS if d in scores]
    if not values:
        return "unknown"
    average = sum(values) / len(values)
    gaps = sum(1 for g in gates.values() if (g or {}).get("result") == "gap")
    if average >= 70 and gaps == 0:
        return "strong"
    if average >= 50:
        return "possible"
    return "weak"


@llm_context("posting_fit")
def evaluate_fit(db: Session, llm: LLMProvider, posting: JobPosting) -> dict:
    set_llm_ids(company_id=posting.company_id)
    profile = db.query(Profile).first()
    records = pre_rank(
        candidate_items(db), f"{posting.title} {posting.description_text} {' '.join(posting.requirements or [])}"
    )
    offered = {item.id for item in records}
    prompt = (
        f"OWNER TARGET ROLES: {', '.join(profile.target_roles) if profile else 'unknown'}\n"
        f"OWNER SUMMARY: {profile.summary if profile else 'unknown'}\n\n"
        "PROFILE RECORDS:\n" + "\n".join(_index_line(item) for item in records) + "\n\n"
        f"POSTING ({posting.title}):\n{wrap_untrusted(posting.description_text, label='JOB POSTING')}"
    )
    response = llm.generate(
        prompt=prompt,
        system=FIT_SYSTEM_PROMPT,
        temperature=0.0,
        max_output_tokens=2000,
        json_schema=FitAnswer.model_json_schema(),
    )
    data = parse_json_object(response.text, fallback={})
    if "parse_error" in data:
        raise ValueError("The fit evaluation answer wasn't valid JSON - try again.")
    answer = FitAnswer.model_validate(data).model_dump()

    scores = {}
    dropped_ids: list[int] = []
    for dimension in DIMENSIONS:
        entry = answer["scores"][dimension]
        ids = [i for i in entry["evidence_ids"] if i in offered]
        dropped_ids += [i for i in entry["evidence_ids"] if i not in offered]
        scores[dimension] = {
            "score": max(0, min(100, entry["score"])),
            "posting_quote": _verified_quote(entry["posting_quote"], posting),
            "evidence_ids": ids,
            "note": entry["note"],
        }
    fit = {
        "scores": scores,
        "strengths": [{**p, "posting_quote": _verified_quote(p["posting_quote"], posting)} for p in answer["strengths"]],
        "gaps": [{**p, "posting_quote": _verified_quote(p["posting_quote"], posting)} for p in answer["gaps"]],
        "dropped_evidence_ids": dropped_ids,
        "verdict": verdict_for(scores, posting.gates or {}),
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }
    posting.fit = fit
    db.commit()
    return fit


def legitimacy_flags(db: Session, posting: JobPosting, repost_of: JobPosting | None = None) -> list[dict]:
    flags = []
    if not posting.company_id and not (posting.company_name or "").strip():
        flags.append({"kind": "no_company", "detail": "The posting doesn't identify the hiring company."})
    if posting.date_posted:
        try:
            posted = datetime.fromisoformat(posting.date_posted[:10]).replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - posted).days
            if age > get_settings().posting_stale_days:
                flags.append({"kind": "old", "detail": f"Posted {age} days ago ({posting.date_posted[:10]})."})
        except ValueError:
            pass
    if repost_of is not None and repost_of.id != posting.id:
        flags.append({"kind": "reposted", "detail": f"Same text as posting #{repost_of.id} (first seen {repost_of.first_seen_at:%Y-%m-%d})."})
    company_site = posting.company.website_url if posting.company else (posting.extra or {}).get("company_website", "")
    if posting.apply_url and company_site:
        apply_domain = registrable_domain(posting.apply_url)
        if apply_domain != registrable_domain(company_site) and classify_ats_url(posting.apply_url) is None:
            flags.append({"kind": "off_domain_apply", "detail": f"Apply link goes to {apply_domain}, not the company's site."})
    return flags
