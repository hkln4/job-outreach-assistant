"""Posting orchestration: intake -> dedup -> parse -> gates -> company match ->
preference check -> legitimacy -> (fit). Each step is owner-triggered from
the UI; nothing here runs in the background."""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.applications.synonyms import fold
from app.channels.detect import classify_ats_url
from app.channels.domains import registrable_domain
from app.companies.context import locate_quote
from app.companies.dedup import CompanyIndex
from app.companies.linkedin_fetch import linkedin_job_id
from app.llm.base import LLMProvider
from app.llm.call_log import llm_context
from app.models import Company, CompanyState, JobPosting, PostingStatus, Preference
from app.postings.evaluation import evaluate_fit, legitimacy_flags
from app.postings.gates import gates_block, run_gates
from app.postings.intake import (
    build_posting,
    content_hash,
    fetch_posting,
    find_existing,
    linkedin_posting_text,
    normalize_posting_url,
    text_from_paste,
)
from app.preferences.checker import PREFERENCE_CONTEXT_CHARS, check_company
from app.preferences.keywords import keyword_flags

AGGREGATOR_DOMAINS = {"kariyer.net", "linkedin.com", "indeed.com", "yenibiris.com", "secretcv.com", "glassdoor.com", "eleman.net", "jooble.org"}
REPOST_AFTER_DAYS = 14


def _match_company(db: Session, posting: JobPosting) -> None:
    index = CompanyIndex(db)
    website = (posting.extra or {}).get("company_website") or ""
    url_domain = registrable_domain(posting.url) if posting.url else ""
    own_site = posting.url if posting.url and url_domain not in AGGREGATOR_DOMAINS and classify_ats_url(posting.url) is None else ""
    company = index.find(posting.company_name or "", website or own_site or None)
    if company is not None:
        posting.company_id = company.id
        posting.company = company
        posting.proposed_company = {}
    elif posting.company_name or website or own_site:
        site = website or (f"https://{url_domain}" if own_site else "")
        posting.proposed_company = {"name": posting.company_name or url_domain, "website_url": site}


def check_posting_preferences(db: Session, llm: LLMProvider, posting: JobPosting) -> list[dict]:
    """Keyword pre-screen by code, then the LLM check on the posting text;
    every flag carries the snippet (located in the posting by code)."""
    preferences = db.query(Preference).all()
    if not preferences:
        posting.preference_check = []
        return []
    pages = [{"url": posting.url or "posting", "text": posting.description_text}]
    flags = []
    for hit in keyword_flags(posting.description_text, preferences, posting.url):
        flags.append({**hit, "matched": True, "severity": hit["type"], "found_by": "keyword", "source_url_verified": True, "source_url": posting.url, "confidence": "keyword"})
    by_id = {p.id: p for p in preferences}
    for flag in check_company(llm, posting.description_text[:PREFERENCE_CONTEXT_CHARS], preferences):
        pref = by_id.get(flag.get("preference_id"))
        if pref is None:
            continue
        snippet = flag.get("evidence_snippet", "")
        flags.append(
            {
                "preference_id": pref.id,
                "description": pref.description,
                "matched": True,
                "severity": pref.type.value,
                "evidence_snippet": snippet,
                "source_url": posting.url,
                "source_url_verified": locate_quote(snippet, pages) is not None,
                "confidence": flag.get("confidence", "low"),
                "found_by": "llm",
            }
        )
    posting.preference_check = flags
    posting.hard_exclusion_confirmed = False
    return flags


def has_unconfirmed_hard_flag(posting: JobPosting) -> bool:
    return any(f.get("severity") == "hard" for f in posting.preference_check or []) and not posting.hard_exclusion_confirmed


def _link_copies(db: Session, posting: JobPosting) -> None:
    """The same job on the employer's site and on an aggregator: link them
    and record which requirements differ (prefer the employer's copy)."""
    if not posting.company_id:
        return
    title = fold(posting.title).strip()
    for other in db.query(JobPosting).filter(JobPosting.company_id == posting.company_id, JobPosting.id != posting.id).all():
        if fold(other.title).strip() != title:
            continue
        mine, theirs = set(posting.requirements or []), set(other.requirements or [])
        for a, b in ((posting, other), (other, posting)):
            copies = [c for c in (a.extra or {}).get("copies", []) if c.get("id") != b.id]
            copies.append({"id": b.id, "url": b.url, "aggregator": registrable_domain(b.url) in AGGREGATOR_DOMAINS,
                           "only_here": sorted((mine - theirs) if a is posting else (theirs - mine)),
                           "only_there": sorted((theirs - mine) if a is posting else (mine - theirs))})
            a.extra = {**(a.extra or {}), "copies": copies}


@llm_context("posting_intake")
def add_posting(
    db: Session,
    llm: LLMProvider,
    *,
    url: str = "",
    text: str = "",
    source: str = "manual",
    evaluate: bool = True,
    extra: dict | None = None,
) -> tuple[JobPosting, bool]:
    """(posting, created). Pasted text wins over fetching; a URL alone is
    fetched (LinkedIn only through the MCP rule). Same URL or same text ->
    the existing posting is returned (and marked seen again)."""
    url = normalize_posting_url(url) if url else ""
    json_ld: dict = {}
    safety: dict = {}
    expired = None
    if text.strip():
        clean, safety = text_from_paste(text)
    elif url and linkedin_job_id(url):
        clean, url = linkedin_posting_text(url)
        source = "linkedin"
    elif url:
        fetched = fetch_posting(url)
        if fetched["status"] >= 400 and not fetched["expired"]:
            raise ValueError(f"The posting page answered HTTP {fetched['status']}.")
        clean, json_ld, safety, expired = fetched["text"], fetched["json_ld"], fetched["safety"], fetched["expired"]
        url = normalize_posting_url(fetched["final_url"]) or url
    else:
        raise ValueError("Give the posting's URL or paste its text.")
    if not expired and len(clean.strip()) < 100:
        raise ValueError("The posting has almost no readable text - paste it instead.")

    digest = content_hash(clean) if clean else ""
    existing, how = find_existing(db, url, digest)
    now = datetime.now(timezone.utc)
    if existing is not None:
        existing.last_seen_at = now
        if how == "hash" and url and url != existing.url:
            others = list(dict.fromkeys([*(existing.extra or {}).get("other_urls", []), url]))
            existing.extra = {**(existing.extra or {}), "other_urls": others}
            first = existing.first_seen_at if existing.first_seen_at.tzinfo else existing.first_seen_at.replace(tzinfo=timezone.utc)
            if (now - first).days >= REPOST_AFTER_DAYS and not any(f["kind"] == "reposted" for f in existing.legitimacy_flags or []):
                existing.legitimacy_flags = [*(existing.legitimacy_flags or []), {"kind": "reposted", "detail": f"Reposted at {url} with the same text."}]
        db.commit()
        return existing, False

    if expired:
        posting = JobPosting(source=source, url=url, title=json_ld.get("title", ""), status=PostingStatus.EXPIRED,
                             description_text=clean, content_hash=digest, extra={"expired": expired, **(extra or {})})
        db.add(posting)
        db.commit()
        return posting, True

    posting = build_posting(llm, text=clean, url=url, source=source, json_ld=json_ld, extra=extra)
    posting.content_safety = safety
    posting.gates = run_gates(posting.title, posting.location, posting.description_text)
    _match_company(db, posting)
    db.add(posting)
    db.flush()
    check_posting_preferences(db, llm, posting)
    posting.legitimacy_flags = legitimacy_flags(db, posting)
    _link_copies(db, posting)
    db.commit()
    if evaluate and not gates_block(posting.gates):
        try:
            evaluate_fit(db, llm, posting)
        except Exception as exc:  # noqa: BLE001 - the posting is saved; the owner can re-run fit
            posting.fit = {"error": str(exc)[:300]}
            db.commit()
    db.refresh(posting)
    return posting, True


def approve_proposed_company(db: Session, posting: JobPosting) -> Company:
    """The owner confirms the posting's company: an existing match, or a new
    APPLICABLE Company from the proposal. Research then runs as usual."""
    proposal = posting.proposed_company or {}
    company = CompanyIndex(db).find(proposal.get("name", ""), proposal.get("website_url") or None)
    if company is None:
        company = Company(
            name=proposal.get("name") or posting.company_name or "Unknown company",
            website_url=proposal.get("website_url") or None,
            state=CompanyState.APPLICABLE,
            notes=f"Added from job posting #{posting.id}.",
        )
        db.add(company)
        db.flush()
    posting.company_id = company.id
    posting.proposed_company = {}
    posting.legitimacy_flags = [f for f in posting.legitimacy_flags or [] if f.get("kind") != "no_company"]
    db.commit()
    return company


def check_expiry(db: Session, posting: JobPosting) -> str | None:
    """Re-fetch the posting URL: 404/410 or a closed-posting phrase -> EXPIRED.
    Returns the reason, or None when it is still up (or has no URL)."""
    if not posting.url or posting.source == "linkedin":
        return None
    try:
        fetched = fetch_posting(posting.url)
    except Exception as exc:  # noqa: BLE001 - unreachable is not the same as closed
        posting.extra = {**(posting.extra or {}), "expiry_check": {"error": str(exc)[:200], "at": datetime.now(timezone.utc).isoformat()}}
        db.commit()
        return None
    reason = fetched["expired"]
    posting.extra = {**(posting.extra or {}), "expiry_check": {"reason": reason or "", "at": datetime.now(timezone.utc).isoformat()}}
    if reason:
        posting.status = PostingStatus.EXPIRED
    else:
        posting.last_seen_at = datetime.now(timezone.utc)
    db.commit()
    return reason


ALLOWED_TRANSITIONS = {
    PostingStatus.NEW: {PostingStatus.SHORTLISTED, PostingStatus.DISMISSED, PostingStatus.EXPIRED, PostingStatus.APPLIED},
    PostingStatus.SHORTLISTED: {PostingStatus.NEW, PostingStatus.DISMISSED, PostingStatus.EXPIRED, PostingStatus.APPLIED},
    PostingStatus.APPLIED: set(),
    PostingStatus.EXPIRED: {PostingStatus.NEW},
    PostingStatus.DISMISSED: {PostingStatus.NEW},
}


def set_posting_status(db: Session, posting: JobPosting, status: PostingStatus) -> None:
    if status != posting.status and status not in ALLOWED_TRANSITIONS[posting.status]:
        raise ValueError(f"A {posting.status.value} posting can't become {status.value}.")
    posting.status = status
    db.commit()


@llm_context("posting_intake")
def parse_and_evaluate(db: Session, llm: LLMProvider, posting: JobPosting) -> JobPosting:
    """For postings that came in unparsed (careers-page listing, ATS board):
    fetch the posting's own page when it has one, parse the fields, then the
    same gates -> preferences -> legitimacy -> fit as a pasted posting."""
    text = posting.description_text or ""
    found_on = (posting.extra or {}).get("found_on")
    if posting.url and posting.source == "careers_page" and posting.url != found_on:
        fetched = fetch_posting(posting.url)
        if fetched["expired"]:
            posting.status = PostingStatus.EXPIRED
            posting.extra = {**(posting.extra or {}), "expired": fetched["expired"]}
            db.commit()
            return posting
        if len(fetched["text"]) > len(text):
            text = fetched["text"]
            posting.content_safety = fetched["safety"]
    parsed = build_posting(llm, text=text, url=posting.url, source=posting.source)
    for field in ("requirements", "nice_to_have", "seniority", "deadline", "work_mode", "language"):
        setattr(posting, field, getattr(parsed, field))
    posting.title = posting.title or parsed.title
    posting.company_name = posting.company_name or parsed.company_name
    posting.location = posting.location or parsed.location
    posting.apply_url = posting.apply_url or parsed.apply_url
    posting.description_text = text
    posting.content_hash = content_hash(text)
    posting.extra = {**(posting.extra or {}), **(parsed.extra or {}), "parsed": True}
    posting.gates = run_gates(posting.title, posting.location, posting.description_text)
    if not posting.company_id:
        _match_company(db, posting)
    check_posting_preferences(db, llm, posting)
    posting.legitimacy_flags = legitimacy_flags(db, posting)
    db.commit()
    if not gates_block(posting.gates):
        evaluate_fit(db, llm, posting)
    db.refresh(posting)
    return posting
