"""Posting intake (roadmap phase 3): owner paste (URL or text), careers-page
change, or public ATS board -> one JobPosting.

Deterministic first: the URL and a hash of the normalized text dedupe before
anything else; schema.org JobPosting JSON-LD (most career pages and ATS boards
embed it) fills title, location, dates and hiring organization by code; the
reading model only parses what is left (requirements, nice-to-haves, work
mode). Pasted text is sanitized exactly like fetched pages - pasting by hand
doesn't make it trusted - and URLs found inside a posting body are never
fetched; only the URL the owner gave.
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urldefrag

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.applications.synonyms import fold
from app.companies.fetch import BROWSER_HEADERS, _extract_main_text
from app.companies.sanitize import SanitizeReport, sanitize_text
from app.config import get_settings
from app.llm.base import LLMProvider
from app.llm.call_log import llm_context
from app.llm.json_utils import parse_json_object
from app.llm.untrusted import UNTRUSTED_RULE, wrap_untrusted
from app.models import JobPosting, PostingStatus
from app.postings.gates import detect_language

MAX_POSTING_CHARS = 20000
EXPIRED_PATTERNS = (
    "ilan yayından kaldırıldı", "ilan yayindan kaldirildi", "bu ilan artık aktif değil", "ilan süresi doldu",
    "başvuru süresi sona erdi", "position closed", "position has been filled", "no longer accepting applications",
    "this job is no longer available", "job has expired", "posting has closed", "this position is closed",
)

PARSE_SYSTEM_PROMPT = """You are parsing ONE job posting into fields. Use only the posting text.
- title: as written. company_name: the hiring company as the text names it ("" if not named).
- location: as written ("" if not stated). work_mode: onsite | hybrid | remote | unknown.
- requirements: each required qualification, copied from the text (short, one per item).
- nice_to_have: each preferred/bonus qualification, copied from the text.
- seniority: intern | junior | mid | senior | lead | unknown, only from what the text says.
- deadline: an application deadline as written, else "".
- apply_url: an application link literally present in the text, else "".
- employment_type: full-time | part-time | internship | contract | unknown.
""" + UNTRUSTED_RULE


class PostingFields(BaseModel):
    title: str
    company_name: str
    location: str
    work_mode: str
    requirements: list[str]
    nice_to_have: list[str]
    seniority: str
    deadline: str
    apply_url: str
    employment_type: str


def normalize_posting_url(url: str) -> str:
    url = urldefrag((url or "").strip())[0]
    url = re.sub(r"[?&](utm_[^=&]+|ref|src|source|trk|refId|trackingId)=[^&]*", "", url)
    return url.rstrip("?&/")


def content_hash(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", fold(text)).strip().encode()).hexdigest()


def is_expired_text(text: str) -> str | None:
    lowered = fold(text)
    for pattern in EXPIRED_PATTERNS:
        if fold(pattern) in lowered:
            return pattern
    return None


def _ld_items(data) -> list[dict]:
    if isinstance(data, list):
        return [x for item in data for x in _ld_items(item)]
    if isinstance(data, dict):
        if "@graph" in data:
            return _ld_items(data["@graph"])
        return [data]
    return []


def json_ld_posting(html: str) -> dict:
    """Fields from an embedded schema.org JobPosting, read by code."""
    soup = BeautifulSoup(html or "", "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except ValueError:
            continue
        for item in _ld_items(data):
            types = item.get("@type")
            if types == "JobPosting" or (isinstance(types, list) and "JobPosting" in types):
                org = item.get("hiringOrganization") or {}
                org = org[0] if isinstance(org, list) and org else org
                locations = item.get("jobLocation") or []
                locations = locations if isinstance(locations, list) else [locations]
                places = []
                for loc in locations:
                    address = (loc or {}).get("address") or {}
                    if isinstance(address, dict):
                        parts = [address.get("addressLocality"), address.get("addressRegion"), address.get("addressCountry")]
                        parts = [p.get("name") if isinstance(p, dict) else p for p in parts]
                        places.append(", ".join(p for p in parts if p))
                remote = str(item.get("jobLocationType") or "").upper() == "TELECOMMUTE"
                description = BeautifulSoup(item.get("description") or "", "html.parser").get_text("\n", strip=True)
                return {
                    "title": item.get("title") or "",
                    "company_name": (org or {}).get("name", "") if isinstance(org, dict) else "",
                    "company_website": (org or {}).get("sameAs", "") if isinstance(org, dict) else "",
                    "location": "; ".join(p for p in places if p) or ("Remote" if remote else ""),
                    "work_mode": "remote" if remote else "",
                    "date_posted": str(item.get("datePosted") or "")[:10],
                    "deadline": str(item.get("validThrough") or "")[:10],
                    "employment_type": str(item.get("employmentType") or "").lower(),
                    "description": description,
                }
    return {}


def fetch_posting(url: str, client: httpx.Client | None = None) -> dict:
    """{url, final_url, status, html, text, json_ld, safety, expired}."""
    own = client is None
    client = client or httpx.Client(timeout=15, follow_redirects=True, headers=BROWSER_HEADERS)
    try:
        resp = client.get(url)
    finally:
        if own:
            client.close()
    safety = SanitizeReport()
    html = resp.text if resp.status_code < 400 else ""
    text = _extract_main_text(html, safety, url=str(resp.url)) if html else ""
    ld = json_ld_posting(html)
    if ld.get("description") and len(ld["description"]) > len(text) / 2:
        text = sanitize_text(ld["description"], safety, url=str(resp.url))
    expired = "http_" + str(resp.status_code) if resp.status_code in (404, 410) else is_expired_text(text)
    return {
        "url": url,
        "final_url": str(resp.url),
        "status": resp.status_code,
        "html": html,
        "text": text[:MAX_POSTING_CHARS],
        "json_ld": ld,
        "safety": safety.to_dict(),
        "expired": expired,
    }


@llm_context("posting_parse")
def parse_posting(llm: LLMProvider, text: str) -> dict:
    response = llm.generate(
        prompt=f"POSTING:\n{wrap_untrusted(text[:MAX_POSTING_CHARS], label='JOB POSTING')}",
        system=PARSE_SYSTEM_PROMPT,
        temperature=0.0,
        max_output_tokens=2000,
        json_schema=PostingFields.model_json_schema(),
    )
    data = parse_json_object(response.text, fallback={})
    if "parse_error" in data:
        raise ValueError("The posting couldn't be parsed (unreadable answer) - try again.")
    return PostingFields.model_validate(data).model_dump()


def _verbatim(items: list[str], text: str) -> tuple[list[str], list[str]]:
    """Requirements the text really contains (whitespace/case-insensitive);
    the rest are returned separately so the owner sees them as unverified."""
    haystack = re.sub(r"\s+", " ", fold(text))
    kept, unverified = [], []
    for item in items:
        needle = re.sub(r"\s+", " ", fold(item)).strip(" .;-•")
        words = needle.split()
        # "copied from the text" in short form: most of its words must be in the text
        found = needle in haystack or (len(words) >= 2 and sum(w in haystack for w in words) / len(words) >= 0.8)
        (kept if found else unverified).append(item)
    return kept, unverified


def find_existing(db: Session, url: str, digest: str) -> tuple[JobPosting | None, str]:
    """(posting, how) - "url" for the same posting again, "hash" for the same
    text under another URL (a repost)."""
    if url:
        normalized = normalize_posting_url(url)
        for posting in db.query(JobPosting).filter(JobPosting.url != "").all():
            if normalize_posting_url(posting.url) == normalized:
                return posting, "url"
    if digest:
        posting = db.query(JobPosting).filter(JobPosting.content_hash == digest).first()
        if posting is not None:
            return posting, "hash"
    return None, ""


def build_posting(
    llm: LLMProvider, *, text: str, url: str = "", source: str = "manual", json_ld: dict | None = None, extra: dict | None = None
) -> JobPosting:
    """A new (unsaved) JobPosting from already-sanitized text."""
    ld = json_ld or {}
    fields = parse_posting(llm, text)
    requirements, unverified_req = _verbatim(fields["requirements"], text)
    nice, unverified_nice = _verbatim(fields["nice_to_have"], text)

    now = datetime.now(timezone.utc)
    return JobPosting(
        source=source,
        url=url,
        apply_url=fields["apply_url"] if fields["apply_url"] and fields["apply_url"] in text else "",
        title=ld.get("title") or fields["title"],
        company_name=ld.get("company_name") or fields["company_name"],
        location=ld.get("location") or fields["location"],
        work_mode=ld.get("work_mode") or fields["work_mode"] or "unknown",
        language=detect_language(text),
        seniority=fields["seniority"] or "unknown",
        description_text=text,
        requirements=requirements,
        nice_to_have=nice,
        deadline=ld.get("deadline") or fields["deadline"],
        date_posted=ld.get("date_posted", ""),
        content_hash=content_hash(text),
        first_seen_at=now,
        last_seen_at=now,
        status=PostingStatus.NEW,
        extra={
            **(extra or {}),
            "unverified_requirements": unverified_req + unverified_nice,
            "company_website": ld.get("company_website", ""),
            "employment_type": ld.get("employment_type") or fields["employment_type"],
        },
    )


def text_from_paste(text: str) -> tuple[str, dict]:
    """Pasted text goes through the same sanitizer as fetched pages."""
    report = SanitizeReport()
    return sanitize_text(text.strip()[:MAX_POSTING_CHARS], report), report.to_dict()


def linkedin_posting_text(url: str) -> tuple[str, str]:
    """(text, source_url) of a LinkedIn job via the MCP server - only with
    LINKEDIN_RESEARCH_ENABLED (throwaway account, one posting per click)."""
    settings = get_settings()
    if not settings.linkedin_research_enabled:
        raise ValueError(
            "LinkedIn job pages are never fetched directly. Turn on LINKEDIN_RESEARCH_ENABLED "
            "(throwaway account) to read it through the MCP server, or paste the posting text."
        )
    from app.companies.linkedin_fetch import fetch_linkedin_job

    result = fetch_linkedin_job(url, command=settings.linkedin_mcp_command, timeout_seconds=settings.linkedin_mcp_timeout_seconds)
    report = SanitizeReport()
    return sanitize_text(result["text"], report), result["source_url"]
