from app.db import SessionLocal, init_db
from app.models import ProfileItemStatus
from app.profile_builder.site_ingest import ingest_site
from tests.fakes import FakeLLMProvider


def setup_module():
    init_db()


def test_ingest_site_survives_unreachable_url_instead_of_crashing():
    """Regression: a bad URL (DNS failure, timeout, 404) must become a
    needs_review item, not an unhandled exception that 500s the request."""
    llm = FakeLLMProvider("should never be called")
    db = SessionLocal()
    try:
        item = ingest_site(db, llm, "https://this-domain-does-not-exist-xyz123.invalid")
        assert item.status == ProfileItemStatus.NEEDS_REVIEW
        assert "parse_error" in item.extracted
        assert len(llm.calls) == 0
    finally:
        db.close()
