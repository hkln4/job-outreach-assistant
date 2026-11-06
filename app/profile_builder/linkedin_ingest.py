"""LinkedIn ingestion (CLAUDE.md section 3): parsed only from the owner's uploaded
"Save to PDF" export or data archive - no LinkedIn scraping."""
from sqlalchemy.orm import Session

from app.llm.base import LLMProvider
from app.models import ProfileItem
from app.profile_builder.cv_parser import try_extract_text
from app.profile_builder.entries import ingest_document_entries


def ingest_linkedin_export(db: Session, llm: LLMProvider, file_path: str) -> list[ProfileItem]:
    text, error = try_extract_text(file_path)
    return ingest_document_entries(db, llm, file_path, text, error, "linkedin")
