from app.db import SessionLocal, init_db
from app.models import ProfileItemStatus
from app.profile_builder import github_ingest
from tests.fakes import FakeLLMProvider


def setup_module():
    init_db()


def test_ingest_repo_survives_api_failure_instead_of_crashing(monkeypatch):
    """Regression: one bad repo (renamed/deleted, network hiccup) must become a
    needs_review item and let the rest of the batch continue, not crash the
    whole ingestion request."""

    class _BrokenGithub:
        def __init__(self, *args, **kwargs):
            pass

        def get_repo(self, full_name):
            raise RuntimeError("404 - repo not found")

    monkeypatch.setattr(github_ingest, "Github", _BrokenGithub)

    llm = FakeLLMProvider("should never be called")
    db = SessionLocal()
    try:
        item = github_ingest.ingest_repo(db, llm, "fake-token", "someone/renamed-repo")
        assert item.status == ProfileItemStatus.NEEDS_REVIEW
        assert "parse_error" in item.extracted
        assert len(llm.calls) == 0
    finally:
        db.close()
