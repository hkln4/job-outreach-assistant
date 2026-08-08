"""Extra sites ingestion (CLAUDE.md section 3): fetch each owner-provided URL,
extract main text, one LLM call per page. Retrieval stays in the app."""
import httpx
from bs4 import BeautifulSoup
from readability import Document as ReadabilityDocument
from sqlalchemy.orm import Session

from app.llm.base import LLMProvider
from app.models import ProfileItem
from app.profile_builder.coverage import record_item
from app.profile_builder.extraction import EXTRACTION_FALLBACK, safe_extract_item

MAX_CHARS = 15000


def fetch_main_text(url: str) -> str:
    resp = httpx.get(
        url, timeout=15, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}
    )
    resp.raise_for_status()
    doc = ReadabilityDocument(resp.text)
    soup = BeautifulSoup(doc.summary(), "html.parser")
    return soup.get_text("\n", strip=True)[:MAX_CHARS]


def ingest_site(db: Session, llm: LLMProvider, url: str) -> ProfileItem:
    # An unreachable/erroring URL must never crash the request - it becomes a
    # needs_review item instead, same as a corrupt CV file.
    try:
        text = fetch_main_text(url)
    except Exception as exc:  # noqa: BLE001 - any fetch failure is recoverable here
        extracted = {**EXTRACTION_FALLBACK, "parse_error": str(exc)}
        return record_item(db, source_type="site", source_ref=url, extracted=extracted, source_urls=[url])

    extracted = safe_extract_item(llm, text)
    return record_item(db, source_type="site", source_ref=url, extracted=extracted, source_urls=[url])
