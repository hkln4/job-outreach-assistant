"""Directory discovery: import a source's companies as candidates, then turn
the ones the owner approves into Companies. Nothing becomes a Company without
an explicit approval, and a rejected candidate is never suggested again."""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.companies.bulk_parse import domain_of
from app.companies.dedup import CompanyIndex
from app.discovery.sources import (
    ANKARA_TEKNOKENT_SITEMAP,
    CYBERPARK_ARCHIVE,
    HACETTEPE_DIRECTORY,
    ODTU_TEKNOKENT_JSON,
    OSTIM_DIRECTORY,
    read_source,
)
from app.llm.base import LLMProvider
from app.models import CandidateStatus, Company, CompanyCandidate, CompanyState, DiscoverySource

DEFAULT_SOURCES = [
    {"name": "ODTÜ Teknokent", "city": "Ankara", "url": ODTU_TEKNOKENT_JSON, "kind": "odtu_json"},
    {"name": "Bilkent Cyberpark", "city": "Ankara", "url": CYBERPARK_ARCHIVE, "kind": "cyberpark"},
    {"name": "Hacettepe Teknokent", "city": "Ankara", "url": HACETTEPE_DIRECTORY, "kind": "hacettepe"},
    {"name": "OSTİM Teknopark", "city": "Ankara", "url": OSTIM_DIRECTORY, "kind": "ostim"},
    {"name": "Ankara Üniversitesi Teknokent", "city": "Ankara", "url": ANKARA_TEKNOKENT_SITEMAP, "kind": "ankara_teknokent"},
]


def ensure_default_sources(db: Session) -> None:
    for spec in DEFAULT_SOURCES:
        if db.query(DiscoverySource).filter(DiscoverySource.url == spec["url"]).first() is None:
            db.add(DiscoverySource(**spec))
    db.commit()


def _backfill(company: Company, city: str, technopark: str, contact_email: str | None = None) -> None:
    """Fill only what's empty - anything the owner entered is never overwritten."""
    if not (company.location or "").strip() and city:
        company.location = city
    if not (company.technopark or "").strip() and technopark:
        company.technopark = technopark
    if not (company.contact_email or "").strip() and contact_email:
        company.contact_email = contact_email


def reconcile_pending(db: Session) -> int:
    """Hide candidates that turn out to be companies already in the list -
    matched now by a looser rule, by a brand name screening found, or because
    the owner added the company by hand since the import. Returns how many."""
    index = CompanyIndex(db)
    matched = 0
    for candidate in db.query(CompanyCandidate).filter(CompanyCandidate.status == CandidateStatus.PENDING).all():
        company = index.find(candidate.name, candidate.website_url, (candidate.triage or {}).get("brand_name"))
        if company is not None:
            _backfill(company, candidate.city, candidate.technopark, candidate.contact_email)
            candidate.status = CandidateStatus.MATCHED
            candidate.company_id = company.id
            matched += 1
    if matched:
        db.commit()
    return matched


def _candidate_key(name: str, website_url: str | None) -> tuple[str, str]:
    return (domain_of(website_url) if website_url else "", name.casefold())


def import_source(db: Session, llm: LLMProvider, source: DiscoverySource) -> dict:
    """Returns counts: {"found", "new", "already_in_list", "known"}."""
    try:
        entries = read_source(llm, source.kind, source.url)
    except Exception as exc:  # noqa: BLE001 - shown on the source row
        source.last_error = str(exc)[:500]
        db.commit()
        raise

    index = CompanyIndex(db)
    existing = db.query(CompanyCandidate).all()
    by_domain = {domain_of(c.website_url): c for c in existing if c.website_url}
    by_name = {c.name.casefold(): c for c in existing}

    counts = {"found": len(entries), "new": 0, "already_in_list": 0, "known": 0}
    for entry in entries:
        company = index.find(entry["name"], entry["website_url"], entry.get("brand_name"))
        if company is not None:
            _backfill(company, source.city, source.name, entry.get("contact_email"))
            counts["already_in_list"] += 1
            continue

        domain, name_key = _candidate_key(entry["name"], entry["website_url"])
        known = (by_domain.get(domain) if domain else None) or by_name.get(name_key)
        if known is not None:  # imported before (any status - a rejection sticks)
            if not known.website_url and entry["website_url"]:
                known.website_url = entry["website_url"]
            if not known.contact_email and entry.get("contact_email"):
                known.contact_email = entry["contact_email"]
            counts["known"] += 1
            continue

        candidate = CompanyCandidate(
            source_id=source.id,
            name=entry["name"],
            website_url=entry["website_url"],
            sector=entry.get("sector") or "",
            contact_email=entry.get("contact_email"),
            city=source.city,
            technopark=source.name,
        )
        db.add(candidate)
        if domain:
            by_domain[domain] = candidate
        by_name[name_key] = candidate
        counts["new"] += 1

    source.last_imported_at = datetime.now(timezone.utc)
    source.last_error = ""
    db.commit()
    return counts


def approve(db: Session, candidate_ids: list[int]) -> int:
    """Each approved candidate becomes an APPLICABLE Company tagged with its
    city/technopark. No research call here - the dashboard's "needs research"
    queue picks these up when the owner gets to them."""
    created = 0
    index = CompanyIndex(db)
    for candidate in db.query(CompanyCandidate).filter(CompanyCandidate.id.in_(candidate_ids)).all():
        if candidate.status != CandidateStatus.PENDING:
            continue
        name = (candidate.triage or {}).get("brand_name") or candidate.name
        company = index.find(name, candidate.website_url)
        if company is None:
            note = f"Added from the {candidate.technopark} directory"
            if name != candidate.name:
                note += f" (listed as {candidate.name})"
            company = Company(
                name=name,
                website_url=candidate.website_url,
                location=candidate.city,
                technopark=candidate.technopark,
                contact_email=candidate.contact_email,
                state=CompanyState.APPLICABLE,
                notes=note + ".",
            )
            db.add(company)
            db.flush()
            index.add(company)
            created += 1
        else:
            _backfill(company, candidate.city, candidate.technopark, candidate.contact_email)
        candidate.status = CandidateStatus.APPROVED
        candidate.company_id = company.id
    db.commit()
    return created


def reject(db: Session, candidate_ids: list[int]) -> int:
    rejected = 0
    for candidate in db.query(CompanyCandidate).filter(CompanyCandidate.id.in_(candidate_ids)).all():
        if candidate.status == CandidateStatus.PENDING:
            candidate.status = CandidateStatus.REJECTED
            rejected += 1
    db.commit()
    return rejected
