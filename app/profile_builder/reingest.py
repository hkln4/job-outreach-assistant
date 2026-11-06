"""Re-run ingestion for one existing ProfileItem's source, updating records in
place (app/profile_builder/coverage.py::record_item), so the owner's edits and
answers survive. An entry split out of a CV/LinkedIn file re-ingests its
parent file."""
from sqlalchemy.orm import Session

from app.config import get_settings
from app.llm.base import LLMProvider
from app.models import ProfileItem
from app.profile_builder.cv_parser import ingest_cv
from app.profile_builder.github_ingest import ingest_repo
from app.profile_builder.linkedin_ingest import ingest_linkedin_export
from app.profile_builder.site_ingest import ingest_site


def reingest_item(db: Session, llm: LLMProvider, draft_llm: LLMProvider, item: ProfileItem) -> None:
    source_type = item.source_type
    source_ref = item.parent_ref or item.source_ref
    if source_type == "cv":
        ingest_cv(db, llm, source_ref)
    elif source_type == "linkedin":
        ingest_linkedin_export(db, llm, source_ref)
    elif source_type == "site":
        ingest_site(db, llm, source_ref)
    elif source_type == "github_repo":
        ingest_repo(db, llm, get_settings().github_token, source_ref, draft_llm=draft_llm)
