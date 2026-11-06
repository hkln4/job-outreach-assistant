"""Extra sites ingestion (CLAUDE.md section 3): fetch each owner-provided URL,
extract main text, one LLM call per page. Retrieval stays in the app.

With `follow_subpages`, project sub-pages linked from the page (same site,
under the same path or named like projects/portfolio/work) are ingested as
their own items with `parent_ref` = the page - one record per project, not one
blob for the whole portfolio. Depth 1, capped at MAX_SUBPAGES.
"""
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app.companies.fetch import BROWSER_HEADERS, _extract_main_text
from app.llm.base import LLMProvider
from app.models import ProfileItem
from app.profile_builder.coverage import record_item
from app.profile_builder.extraction import EXTRACTION_FALLBACK, safe_extract_item

MAX_CHARS = 15000
MAX_SUBPAGES = 8
SUBPAGE_HINTS = ("project", "proje", "portfolio", "work", "case", "calisma", "çalışma")


def _fetch(url: str) -> str:
    resp = httpx.get(url, timeout=15, follow_redirects=True, headers=BROWSER_HEADERS)
    resp.raise_for_status()
    return resp.text


def fetch_main_text(url: str) -> str:
    return _extract_main_text(_fetch(url), url=url)[:MAX_CHARS]


def project_subpages(html: str, page_url: str) -> list[str]:
    """Same-site links that look like project pages, in page order."""
    root = urlparse(page_url)
    root_path = root.path.rstrip("/")
    found: list[str] = []
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urldefrag(urljoin(page_url, href))[0].rstrip("/")
        parsed = urlparse(url)
        if parsed.netloc.removeprefix("www.") != root.netloc.removeprefix("www.") or url == page_url.rstrip("/"):
            continue
        path = parsed.path.lower()
        under_root = bool(root_path) and path.startswith(root_path.lower() + "/")
        if (under_root or any(hint in path for hint in SUBPAGE_HINTS)) and url not in found:
            found.append(url)
        if len(found) >= MAX_SUBPAGES:
            break
    return found


def _ingest_one(db: Session, llm: LLMProvider, url: str, html: str | None, parent_ref: str | None) -> ProfileItem:
    try:
        text = _extract_main_text(html if html is not None else _fetch(url), url=url)[:MAX_CHARS]
    except Exception as exc:  # noqa: BLE001 - any fetch failure is recoverable here
        extracted = {**EXTRACTION_FALLBACK, "parse_error": str(exc)}
    else:
        extracted = safe_extract_item(llm, text)
    return record_item(db, source_type="site", source_ref=url, extracted=extracted, source_urls=[url], parent_ref=parent_ref)


def ingest_site(db: Session, llm: LLMProvider, url: str, follow_subpages: bool = False) -> ProfileItem:
    """The page's own item; with follow_subpages, its project sub-pages too."""
    # An unreachable/erroring URL must never crash the request - it becomes a
    # needs_review item instead, same as a corrupt CV file.
    try:
        html = _fetch(url)
    except Exception as exc:  # noqa: BLE001
        extracted = {**EXTRACTION_FALLBACK, "parse_error": str(exc)}
        return record_item(db, source_type="site", source_ref=url, extracted=extracted, source_urls=[url])

    item = _ingest_one(db, llm, url, html, None)
    if follow_subpages:
        for sub_url in project_subpages(html, url):
            _ingest_one(db, llm, sub_url, None, url)
    return item
