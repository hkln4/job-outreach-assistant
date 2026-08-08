"""LinkedIn ingestion (CLAUDE.md section 3): parsed only from the owner's uploaded
"Save to PDF" export or data archive - no LinkedIn scraping."""
from sqlalchemy.orm import Session

from app.llm.base import LLMProvider
from app.models import ProfileItem
from app.profile_builder.coverage import record_item
from app.profile_builder.cv_parser import try_extract_text
from app.profile_builder.extraction import EXTRACTION_FALLBACK, safe_extract_item


def ingest_linkedin_export(db: Session, llm: LLMProvider, file_path: str) -> ProfileItem:
    text, error = try_extract_text(file_path)
    if error is not None:
        extracted = {**EXTRACTION_FALLBACK, "parse_error": error}
    else:
        extracted = safe_extract_item(llm, text)
    return record_item(
        db, source_type="linkedin", source_ref=file_path, extracted=extracted, source_urls=[]
    )
