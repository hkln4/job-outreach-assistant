"""CV / LinkedIn-export ingestion (CLAUDE.md section 3): each uploaded PDF/docx file
is one item, parsed in full - the app enumerates and processes, never skims."""
from pathlib import Path

from docx import Document
from pypdf import PdfReader
from sqlalchemy.orm import Session

from app.llm.base import LLMProvider
from app.models import ProfileItem
from app.profile_builder.coverage import record_item
from app.profile_builder.extraction import EXTRACTION_FALLBACK, safe_extract_item


def extract_text(file_path: str) -> str:
    suffix = Path(file_path).suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf_text(file_path)
    if suffix == ".docx":
        return _extract_docx_text(file_path)
    raise ValueError(f"Unsupported file type for CV/profile source: {suffix}")


def _extract_pdf_text(file_path: str) -> str:
    reader = PdfReader(file_path)
    return "\n\n".join(page.extract_text() or "" for page in reader.pages).strip()


def _extract_docx_text(file_path: str) -> str:
    doc = Document(file_path)
    return "\n".join(p.text for p in doc.paragraphs).strip()


def try_extract_text(file_path: str) -> tuple[str | None, str | None]:
    """Returns (text, error) - exactly one is None. A corrupt/unreadable file
    must never crash the request; it becomes a needs_review item instead."""
    try:
        return extract_text(file_path), None
    except Exception as exc:  # noqa: BLE001 - any parse failure is recoverable here
        return None, str(exc)


def ingest_cv(db: Session, llm: LLMProvider, file_path: str) -> ProfileItem:
    text, error = try_extract_text(file_path)
    if error is not None:
        extracted = {**EXTRACTION_FALLBACK, "parse_error": error}
    else:
        extracted = safe_extract_item(llm, text)
    return record_item(db, source_type="cv", source_ref=file_path, extracted=extracted, source_urls=[])
