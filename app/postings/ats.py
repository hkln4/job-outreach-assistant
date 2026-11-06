"""Public ATS job boards (roadmap phase 3): postings read fresh from each
provider's public JSON endpoint, never from search results or HTML copies.

Endpoint shapes were checked against the providers' public docs; the parsers
are defensive (missing fields become ""), so a shape change degrades to
fewer fields, not a crash. Re-verify an endpoint before relying on it if a
board suddenly returns nothing.

Every posting goes through the Turkey location filter by code
(app/postings/gates.py): accepted ones are imported, ambiguous ones (EMEA,
bare "Remote") are shown for the owner to pick, rejected ones are counted.
Imported postings are built from the board's own structured fields - no LLM;
requirements are parsed only when the owner evaluates one.
"""
from datetime import datetime, timezone
from html import unescape

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app.companies.fetch import BROWSER_HEADERS
from app.companies.sanitize import SanitizeReport, sanitize_text
from app.models import AtsBoard, JobPosting, PostingStatus
from app.postings.gates import detect_language, location_gate, owner_targets, role_gate, run_gates
from app.postings.intake import content_hash, find_existing, normalize_posting_url

SUPPORTED_PROVIDERS = ("greenhouse", "lever", "ashby", "smartrecruiters")
MAX_POSTINGS_PER_BOARD = 200


def _html_text(html: str) -> str:
    return BeautifulSoup(unescape(html or ""), "html.parser").get_text("\n", strip=True)


def _greenhouse(client: httpx.Client, token: str) -> list[dict]:
    data = client.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", params={"content": "true"}).raise_for_status().json()
    return [
        {
            "external_id": str(job.get("id", "")),
            "title": job.get("title", ""),
            "location": (job.get("location") or {}).get("name", ""),
            "url": job.get("absolute_url", ""),
            "apply_url": job.get("absolute_url", ""),
            "description": _html_text(job.get("content", "")),
            "posted": (job.get("updated_at") or job.get("first_published") or "")[:10],
        }
        for job in data.get("jobs", [])
    ]


def _lever(client: httpx.Client, token: str) -> list[dict]:
    data = client.get(f"https://api.lever.co/v0/postings/{token}", params={"mode": "json"}).raise_for_status().json()
    postings = []
    for job in data if isinstance(data, list) else []:
        categories = job.get("categories") or {}
        lists = "\n".join(f"{item.get('text', '')}\n{_html_text(item.get('content', ''))}" for item in job.get("lists") or [])
        created = job.get("createdAt")
        postings.append(
            {
                "external_id": job.get("id", ""),
                "title": job.get("text", ""),
                "location": categories.get("location", "") or ", ".join(categories.get("allLocations") or []),
                "work_mode": (job.get("workplaceType") or "").lower(),
                "url": job.get("hostedUrl", ""),
                "apply_url": job.get("applyUrl", ""),
                "description": f"{job.get('descriptionPlain', '')}\n{lists}\n{job.get('additionalPlain', '')}".strip(),
                "posted": datetime.fromtimestamp(created / 1000, timezone.utc).date().isoformat() if isinstance(created, (int, float)) else "",
            }
        )
    return postings


def _ashby(client: httpx.Client, token: str) -> list[dict]:
    data = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{token}").raise_for_status().json()
    postings = []
    for job in data.get("jobs", []):
        locations = [job.get("location", "")] + [(s or {}).get("location", "") for s in job.get("secondaryLocations") or []]
        postings.append(
            {
                "external_id": job.get("id", ""),
                "title": job.get("title", ""),
                "location": "; ".join(l for l in locations if l) + ("; Remote" if job.get("isRemote") and "remote" not in " ".join(locations).lower() else ""),
                "work_mode": (job.get("workplaceType") or "").lower(),
                "url": job.get("jobUrl", ""),
                "apply_url": job.get("applyUrl", ""),
                "description": job.get("descriptionPlain") or _html_text(job.get("descriptionHtml", "")),
                "posted": (job.get("publishedAt") or "")[:10],
            }
        )
    return postings


def _smartrecruiters(client: httpx.Client, token: str) -> list[dict]:
    data = client.get(f"https://api.smartrecruiters.com/v1/companies/{token}/postings").raise_for_status().json()
    postings = []
    for job in data.get("content", []):
        location = job.get("location") or {}
        place = ", ".join(p for p in (location.get("city"), location.get("country")) if p)
        if location.get("remote"):
            place = f"{place}; Remote" if place else "Remote"
        postings.append(
            {
                "external_id": job.get("id", ""),
                "title": job.get("name", ""),
                "location": place,
                "url": f"https://jobs.smartrecruiters.com/{token}/{job.get('id', '')}",
                "apply_url": "",
                "description": "",
                "detail_ref": job.get("ref", ""),
                "posted": (job.get("releasedDate") or "")[:10],
            }
        )
    return postings


def _smartrecruiters_description(client: httpx.Client, ref: str) -> str:
    try:
        data = client.get(ref).raise_for_status().json()
    except (httpx.HTTPError, ValueError):
        return ""
    sections = ((data.get("jobAd") or {}).get("sections") or {}).values()
    return "\n".join(f"{s.get('title', '')}\n{_html_text(s.get('text', ''))}" for s in sections if isinstance(s, dict))


READERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby, "smartrecruiters": _smartrecruiters}


def read_board(provider: str, token: str, client: httpx.Client | None = None) -> list[dict]:
    if provider not in READERS:
        raise ValueError(f"{provider} has no public postings API here - its board is read as a careers page.")
    own = client is None
    client = client or httpx.Client(timeout=20, follow_redirects=True, headers={**BROWSER_HEADERS, "Accept": "application/json"})
    try:
        postings = READERS[provider](client, token)[:MAX_POSTINGS_PER_BOARD]
        for posting in postings:
            posting["location_gate"] = location_gate(posting["location"], posting["description"])
            if provider == "smartrecruiters" and posting["location_gate"]["result"] != "reject" and posting.get("detail_ref"):
                posting["description"] = _smartrecruiters_description(client, posting["detail_ref"])
                posting["location_gate"] = location_gate(posting["location"], posting["description"])
        return postings
    finally:
        if own:
            client.close()


def import_board_postings(db: Session, board: AtsBoard, postings: list[dict], include_ids: set[str] | None = None) -> dict:
    """Accepted postings (and the ambiguous ones the owner picked) become
    JobPostings from the board's own fields. Returns counts."""
    counts = {"imported": 0, "already_known": 0, "ambiguous": 0, "rejected": 0, "not_relevant": 0}
    targets, skills = owner_targets(db)
    for job in postings:
        if role_gate(job["title"], job["description"], targets, skills)["result"] == "reject":
            counts["not_relevant"] += 1  # sales, finance, HR ... never imported
            continue
        result = job["location_gate"]["result"]
        if result == "reject":
            counts["rejected"] += 1
            continue
        if result in ("ambiguous", "unknown") and job["external_id"] not in (include_ids or set()):
            counts["ambiguous"] += 1
            continue
        report = SanitizeReport()
        text = sanitize_text(f"{job['title']}\n{job['location']}\n\n{job['description']}", report)
        digest = content_hash(text)
        existing, _ = find_existing(db, job["url"], digest)
        if existing is not None:
            existing.last_seen_at = datetime.now(timezone.utc)
            counts["already_known"] += 1
            continue
        posting = JobPosting(
            company_id=board.company_id,
            source="ats_api",
            url=normalize_posting_url(job["url"]),
            apply_url=job.get("apply_url", ""),
            title=job["title"],
            company_name=board.company_name,
            location=job["location"],
            work_mode=job.get("work_mode") or "unknown",
            language=detect_language(text),
            description_text=text,
            date_posted=job.get("posted", ""),
            content_hash=digest,
            status=PostingStatus.NEW,
            inbox=True,
            content_safety=report.to_dict(),
            extra={"ats": {"provider": board.provider, "token": board.token, "id": job["external_id"]}, "parsed": False},
        )
        posting.gates = run_gates(posting.title, posting.location, posting.description_text, targets, skills)
        db.add(posting)
        counts["imported"] += 1
    board.last_checked_at = datetime.now(timezone.utc)
    board.last_result = counts
    db.commit()
    return counts


def boards_from_research(db: Session, company) -> list[AtsBoard]:
    """ATS boards detected on a company's own pages (research.channels_detected)
    as AtsBoard rows."""
    boards = []
    for channel in (company.research or {}).get("channels_detected") or []:
        if channel.get("kind") != "ats_board" or not channel.get("token"):
            continue
        board = db.query(AtsBoard).filter(AtsBoard.provider == channel["provider"], AtsBoard.token == channel["token"]).first()
        if board is None:
            board = AtsBoard(provider=channel["provider"], token=channel["token"], url=channel["url"], company_id=company.id,
                             company_name=company.name, found_via="site")
            db.add(board)
        elif board.company_id is None:
            board.company_id, board.company_name = company.id, company.name
        boards.append(board)
    db.commit()
    return boards
