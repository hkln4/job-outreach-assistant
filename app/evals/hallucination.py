"""Hallucination eval: run the real drafting pipeline on trap cases and score
what comes out - by code, never by asking the model whether it lied.

Each case (app/evals/cases/hallucination.json) is a company, its pages, the
owner's records and optionally a posting, built so that a model that
hallucinates has an obvious thing to make up: a technology the owner doesn't
have, a metric his project never measured, a model-written "deployed to 500
users" angle, a research summary sentence no page says, company facts the
site never states, years of experience he doesn't have. `forbidden_patterns`
are the regexes such a fabrication would match.

Per case and run, scored on the letter body (signature and app-written lines
excluded):
- trap_hits: forbidden patterns found - the ground truth, independent of the
  model's own annotations;
- unsupported: sentences the code-side claim check couldn't verify;
- ungrounded: numbers / technologies not in any source;
- banned: stock phrases.
A case passes when all four are zero. Every case runs in its own in-memory
database, so the owner's data is never read or written (LLM call costs are
still logged to the app's /costs page).
"""
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.applications.drafting import create_draft_application, letter_core
from app.applications.phrases import found_in
from app.applications.synonyms import fold
from app.companies.workflow import verify_positions, verify_summary
from app.db import Base
from app.llm.base import LLMProvider
from app.llm.call_log import llm_context
from app.models import CV, Company, CompanyState, JobPosting, OwnerSettings, PostingStatus, Profile, ProfileItem, ProfileItemStatus

CASES_PATH = Path(__file__).parent / "cases" / "hallucination.json"


def load_cases(path: Path | str = CASES_PATH) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def seed(db: Session, case: dict) -> tuple[Company, JobPosting | None]:
    """The case as database rows (a fresh in-memory DB per case)."""
    db.add(Profile(summary=case.get("profile_summary", "Computer engineering graduate."), skills=[], target_roles=case.get("target_roles", ["ML Engineer"])))
    db.add(OwnerSettings(email_language=case.get("language", ""), full_name="Eval Owner", email="owner@eval.example"))
    db.add(CV(name="Eval CV", target_role="ML Engineer", file_path="eval-cv.pdf"))
    spec = case["company"]
    pages = spec["pages"]
    research = {
        "pages": pages,
        "content_chars": sum(len(p["text"]) for p in pages),
        "summary": spec.get("summary", ""),
        "summary_verified": verify_summary(spec.get("summary_evidence") or [], pages),
        "open_positions": verify_positions(spec.get("open_positions") or [], pages),
    }
    company = Company(name=spec["name"], website_url=spec["website_url"], contact_email=spec.get("contact_email"), state=CompanyState.APPLICABLE, research=research)
    db.add(company)
    for record in case["records"]:
        db.add(ProfileItem(status=ProfileItemStatus.ANALYZED, source_type=record["source_type"], source_ref=record["source_ref"],
                           parent_ref=record.get("parent_ref"), source_urls=record.get("source_urls", []), extracted=record["extracted"],
                           facts=record.get("facts", {}), owner_facts=record.get("owner_facts", [])))
    db.commit()
    posting = None
    if case.get("posting"):
        p = case["posting"]
        posting = JobPosting(company_id=company.id, title=p["title"], url=p.get("url", ""), description_text=p["text"], requirements=p.get("requirements", []),
                             language=p.get("language", ""), status=PostingStatus.NEW)
        db.add(posting)
        db.commit()
    return company, posting


def score(body: str, case: dict, notes: dict) -> dict:
    folded = fold(body)
    trap_hits = [pattern for pattern in case.get("forbidden_patterns", []) if re.search(pattern, folded, re.IGNORECASE)]
    claim_check = notes.get("claim_check") or {}
    unsupported = claim_check.get("unsupported") or []
    misses = notes.get("grounding_misses") or {}
    ungrounded = list(misses.get("numbers") or []) + list(misses.get("technologies") or [])
    banned = found_in(body)
    return {
        "trap_hits": trap_hits,
        "unsupported": unsupported,
        "sentences": claim_check.get("checked", 0),
        "claim_check_ran": not claim_check.get("not_checked", False) and bool(claim_check),
        "ungrounded": ungrounded,
        "banned": banned,
        "passed": not (trap_hits or unsupported or ungrounded or banned),
    }


@dataclass
class CaseResult:
    case_id: str
    run: int
    body: str = ""
    error: str = ""
    scores: dict = field(default_factory=dict)


@llm_context("hallucination_eval")
def run_case(case: dict, draft_llm: LLMProvider, run: int = 0) -> CaseResult:
    """One case through the same path the app uses (draft model for angles,
    letter and metadata; the reviewer pass is left out - it never changes
    the letter)."""
    db = _session()
    try:
        company, posting = seed(db, case)
        application = create_draft_application(db, draft_llm, company, job_posting=posting)
        body = letter_core(db, application)
        notes = (application.revision_history or [{}])[0]
        return CaseResult(case["id"], run, body=body, scores=score(body, case, notes))
    except Exception as exc:  # noqa: BLE001 - a failed case is reported, the rest still run
        return CaseResult(case["id"], run, error=f"{type(exc).__name__}: {exc}"[:500])
    finally:
        db.close()


def summarize(results: list[CaseResult]) -> dict:
    scored = [r for r in results if not r.error]
    sentences = sum(r.scores["sentences"] for r in scored)
    unsupported = sum(len(r.scores["unsupported"]) for r in scored)
    return {
        "runs": len(results),
        "errors": sum(1 for r in results if r.error),
        "pass_rate": round(sum(r.scores["passed"] for r in scored) / len(scored), 3) if scored else 0.0,
        "trap_hit_rate": round(sum(bool(r.scores["trap_hits"]) for r in scored) / len(scored), 3) if scored else 0.0,
        "unsupported_sentence_rate": round(unsupported / sentences, 3) if sentences else 0.0,
        "letters_with_ungrounded": sum(bool(r.scores["ungrounded"]) for r in scored),
        "claim_check_coverage": round(sum(r.scores["claim_check_ran"] for r in scored) / len(scored), 3) if scored else 0.0,
    }


def run_eval(cases: list[dict], draft_llm: LLMProvider, runs: int = 1) -> dict:
    results = [run_case(case, draft_llm, run) for run in range(runs) for case in cases]
    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "summary": summarize(results),
        "results": [r.__dict__ for r in results],
    }


def markdown_report(report: dict) -> str:
    s = report["summary"]
    lines = [
        f"# Hallucination eval - {report['at'][:16]}",
        "",
        f"- runs: {s['runs']} (errors: {s['errors']})",
        f"- pass rate: {s['pass_rate']:.0%}",
        f"- letters with a trap hit (fabrication): {s['trap_hit_rate']:.0%}",
        f"- unsupported sentences: {s['unsupported_sentence_rate']:.1%}",
        f"- letters with ungrounded numbers/technologies: {s['letters_with_ungrounded']}",
        f"- sentence check ran on: {s['claim_check_coverage']:.0%} of letters",
        "",
        "| case | run | passed | trap hits | unsupported | ungrounded | banned |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in report["results"]:
        if r["error"]:
            lines.append(f"| {r['case_id']} | {r['run']} | error | {r['error'][:80]} | | | |")
            continue
        sc = r["scores"]
        lines.append(
            f"| {r['case_id']} | {r['run']} | {'yes' if sc['passed'] else 'no'} | {len(sc['trap_hits'])} | "
            f"{len(sc['unsupported'])}/{sc['sentences']} | {', '.join(sc['ungrounded']) or '-'} | {', '.join(sc['banned']) or '-'} |"
        )
    lines.append("")
    for r in report["results"]:
        if r["error"] or r["scores"]["passed"]:
            continue
        lines.append(f"## {r['case_id']} (run {r['run']})")
        for hit in r["scores"]["trap_hits"]:
            lines.append(f"- trap: `{hit}`")
        for entry in r["scores"]["unsupported"]:
            lines.append(f"- unsupported ({entry['reason']}): {entry['sentence']}")
        lines += ["", "```", r["body"], "```", ""]
    return "\n".join(lines)
