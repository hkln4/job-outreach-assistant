"""Careers-page watch (roadmap phase 3): owner-triggered "check careers pages"
for APPLICABLE companies. Each careers page is re-fetched (robots.txt
respected - this is a recurring fetch), its extracted text hashed, and only
when the hash changed does the reading model list the postings on it. New
ones land in the dashboard's "new postings" queue. An unchanged page costs
one HTTP request and no LLM call.
"""
import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.applications.synonyms import fold
from app.companies.fetch import BROWSER_HEADERS, _extract_main_text, _same_site
from app.llm.base import LLMProvider
from app.llm.call_log import llm_context, set_llm_ids
from app.llm.json_utils import parse_json_object
from app.llm.untrusted import UNTRUSTED_RULE, wrap_untrusted
from app.models import Company, CompanyState, JobPosting, PostingStatus
from app.postings.gates import detect_language, owner_targets, role_gate, run_gates
from app.postings.intake import content_hash, find_existing, normalize_posting_url

LIST_SYSTEM_PROMPT = """List the job postings on this careers page. For each: "title" as written, \
"url" ONLY if it is one of the LINKS given (else ""), "details" a short quote of what the page \
says about it (location, requirements) or "". Empty list if the page lists no openings.
""" + UNTRUSTED_RULE


class _Listed(BaseModel):
    title: str
    url: str
    details: str


class ListedAnswer(BaseModel):
    postings: list[_Listed]


def robots_allows(client: httpx.Client, url: str) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        resp = client.get(robots_url)
    except httpx.HTTPError:
        return True
    if resp.status_code >= 400:
        return True
    parser = RobotFileParser()
    parser.parse(resp.text.splitlines())
    return parser.can_fetch(BROWSER_HEADERS["User-Agent"], url) and parser.can_fetch("*", url)


def _page_hash(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", fold(text)).encode()).hexdigest()


@llm_context("careers_watch")
def _list_postings(llm: LLMProvider, text: str, links: list[str]) -> list[dict]:
    response = llm.generate(
        prompt=f"LINKS:\n" + "\n".join(links[:150]) + f"\n\nCAREERS PAGE:\n{wrap_untrusted(text[:15000])}",
        system=LIST_SYSTEM_PROMPT,
        temperature=0.0,
        max_output_tokens=1500,
        json_schema=ListedAnswer.model_json_schema(),
    )
    data = parse_json_object(response.text, fallback={"postings": []})
    if "parse_error" in data:
        raise ValueError("The careers page listing answer wasn't valid JSON.")
    return ListedAnswer.model_validate(data).model_dump()["postings"]


def watch_company(db: Session, llm: LLMProvider, company: Company, client: httpx.Client) -> dict:
    """{"status": unchanged | changed | first | blocked | error, "new": n}."""
    set_llm_ids(company_id=company.id)
    url = company.careers_url
    if not url:
        return {"status": "no_careers_page", "new": 0}
    research = dict(company.research or {})
    watch = dict(research.get("careers_watch") or {})
    if not robots_allows(client, url):
        watch.update({"status": "blocked_by_robots", "checked_at": datetime.now(timezone.utc).isoformat()})
        research["careers_watch"] = watch
        company.research = research
        db.commit()
        return {"status": "blocked", "new": 0}
    try:
        resp = client.get(url)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        watch.update({"status": "error", "error": str(exc)[:200], "checked_at": datetime.now(timezone.utc).isoformat()})
        research["careers_watch"] = watch
        company.research = research
        db.commit()
        return {"status": "error", "new": 0}

    text = _extract_main_text(resp.text, url=url)
    digest = _page_hash(text)
    previous = watch.get("hash")
    watch.update({"hash": digest, "checked_at": datetime.now(timezone.utc).isoformat()})
    if previous == digest:
        watch["status"] = "unchanged"
        research["careers_watch"] = watch
        company.research = research
        db.commit()
        return {"status": "unchanged", "new": 0}

    links = []
    for a in BeautifulSoup(resp.text, "html.parser").find_all("a", href=True):
        link = normalize_posting_url(urljoin(url, a["href"]))
        if link.startswith("http") and _same_site(link, url) and link not in links:
            links.append(link)
    new = 0
    targets, skills = owner_targets(db)
    for listed in _list_postings(llm, text, links):
        if role_gate(listed["title"], listed["details"], targets, skills)["result"] == "reject":
            continue  # not the owner's kind of job
        posting_url = listed["url"] if listed["url"] in links else ""
        description = f"{listed['title']}\n{listed['details']}".strip()
        digest_posting = content_hash(description)
        existing, _ = find_existing(db, posting_url, digest_posting if not posting_url else "")
        if existing is not None:
            existing.last_seen_at = datetime.now(timezone.utc)
            continue
        posting = JobPosting(
            company_id=company.id,
            company_name=company.name,
            source="careers_page",
            url=posting_url or url,
            title=listed["title"],
            description_text=description,
            language=detect_language(description),
            content_hash=digest_posting,
            status=PostingStatus.NEW,
            extra={"parsed": False, "found_on": url},
        )
        posting.gates = run_gates(posting.title, "", description, targets, skills)
        db.add(posting)
        new += 1
    watch["status"] = "first" if previous is None else "changed"
    research["careers_watch"] = watch
    company.research = research
    db.commit()
    return {"status": watch["status"], "new": new}


def watch_all(db: Session, llm: LLMProvider, client: httpx.Client | None = None) -> dict:
    own = client is None
    client = client or httpx.Client(timeout=15, follow_redirects=True, headers=BROWSER_HEADERS)
    summary = {"checked": 0, "changed": 0, "new_postings": 0, "skipped": 0}
    try:
        for company in db.query(Company).filter(Company.state == CompanyState.APPLICABLE).all():
            result = watch_company(db, llm, company, client)
            if result["status"] in ("no_careers_page", "blocked", "error"):
                summary["skipped"] += 1
                continue
            summary["checked"] += 1
            summary["changed"] += result["status"] in ("changed", "first")
            summary["new_postings"] += result["new"]
    finally:
        if own:
            client.close()
    return summary
