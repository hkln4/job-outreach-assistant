"""ATS board discovery through a search API (roadmap phase 3): search -> board
-> API. A search result is used only to find a board token (company slug) -
never as the source of a posting: search indexes lag by days and keep closed
postings. Each token found becomes an AtsBoard whose postings are then read
fresh from the board's public API.

Flag-gated (SEARCH_API_ENABLED + SEARCH_API_KEY), owner-triggered, through a
search API with its own key (Brave Search) - never by scraping a search
engine's HTML.
"""
import httpx
from sqlalchemy.orm import Session

from app.channels.detect import classify_ats_url
from app.config import get_settings
from app.models import AtsBoard

DEFAULT_QUERIES = (
    "site:jobs.lever.co Istanbul",
    "site:jobs.lever.co Turkey",
    "site:jobs.ashbyhq.com Turkey",
    "site:jobs.ashbyhq.com Istanbul",
    "site:boards.greenhouse.io Türkiye",
    "site:boards.greenhouse.io Istanbul",
    "site:job-boards.greenhouse.io Turkey",
    "site:jobs.smartrecruiters.com Istanbul",
)
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"


class SearchNotConfigured(RuntimeError):
    pass


def search_urls(query: str, client: httpx.Client | None = None) -> list[str]:
    settings = get_settings()
    if not settings.search_api_enabled or not settings.search_api_key:
        raise SearchNotConfigured("Set SEARCH_API_ENABLED=true and SEARCH_API_KEY (Brave Search) in .env.")
    if settings.search_api_provider != "brave":
        raise SearchNotConfigured(f"Unknown SEARCH_API_PROVIDER {settings.search_api_provider!r} (supported: brave).")
    own = client is None
    client = client or httpx.Client(timeout=20)
    try:
        resp = client.get(
            BRAVE_URL,
            params={"q": query, "count": 20},
            headers={"Accept": "application/json", "X-Subscription-Token": settings.search_api_key},
        )
        resp.raise_for_status()
        return [r.get("url", "") for r in (resp.json().get("web") or {}).get("results", []) if r.get("url")]
    finally:
        if own:
            client.close()


def discover_boards(db: Session, queries: list[str] | tuple[str, ...] = DEFAULT_QUERIES, client: httpx.Client | None = None) -> dict:
    """{"queries", "urls", "new_boards": [...], "known"} - tokens only."""
    new, known, urls = [], 0, 0
    for query in queries:
        for url in search_urls(query, client):
            urls += 1
            ats = classify_ats_url(url)
            if ats is None or ats["kind"] != "ats_board" or not ats["token"]:
                continue
            exists = db.query(AtsBoard).filter(AtsBoard.provider == ats["provider"], AtsBoard.token == ats["token"]).first()
            if exists is not None or any(b.provider == ats["provider"] and b.token == ats["token"] for b in new):
                known += 1
                continue
            board = AtsBoard(provider=ats["provider"], token=ats["token"], url=url, company_name=ats["token"], found_via="search")
            db.add(board)
            new.append(board)
    db.commit()
    return {"queries": len(queries), "urls": urls, "new_boards": new, "known": known}
