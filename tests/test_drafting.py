import json

import pytest

from app.applications.drafting import create_draft_application, regenerate_draft
from app.db import SessionLocal, init_db
from app.models import CV, ApplicationState, Company, Profile
from tests.fakes import FakeLLMProvider

# Fixtures need enough text to read as a real page (see MIN_USABLE_RESEARCH_CHARS).
PAGE_PAD = " The company works with logistics and manufacturing customers across Turkey, runs its own engineering team in Ankara, and publishes case studies about the systems it has delivered over the past several years."

DRAFT_JSON = json.dumps(
    {
        "target_position": "Backend Engineer",
        "cv_choice_name": "Backend CV",
        "reasoning": "Matches their Python/Postgres stack.",
        "subject": "Application for Backend Engineer",
        "cover_letter_body": "Dear Acme, ...",
        "sources": ["https://acme.example/careers"],
    }
)


def setup_module():
    init_db()


def _seed(db):
    profile = Profile(summary="Backend engineer.", skills=[], target_roles=["Backend Engineer"])
    cv = CV(name="Backend CV", target_role="Backend Engineer", file_path="data/cv_variants/backend.pdf")
    company = Company(
        name="Acme",
        website_url="https://acme.example",
        contact_email="jobs@acme.example",
        research={"pages": [{"url": "https://acme.example/careers", "text": "We use Python and Postgres." + PAGE_PAD}]},
    )
    db.add_all([profile, cv, company])
    db.commit()
    db.refresh(company)
    return company


def test_create_draft_application_links_cv_and_uses_evidence():
    db = SessionLocal()
    try:
        company = _seed(db)
        llm = FakeLLMProvider(DRAFT_JSON)
        application = create_draft_application(db, llm, company)

        assert application.state == ApplicationState.PENDING_APPROVAL
        assert application.cv.name == "Backend CV"
        assert application.sources == ["https://acme.example/careers"]
        assert application.revision_history[0]["reasoning"].startswith("Matches")
    finally:
        db.close()


def test_regenerate_draft_appends_revision_history_and_resets_state():
    db = SessionLocal()
    try:
        company = _seed(db)
        llm = FakeLLMProvider(DRAFT_JSON)
        application = create_draft_application(db, llm, company)

        revised_json = DRAFT_JSON.replace("Dear Acme, ...", "Dear Acme, revised body.")
        llm2 = FakeLLMProvider(revised_json)
        regenerate_draft(db, llm2, application, feedback="Make it shorter.")

        assert application.cover_letter_body == "Dear Acme, revised body."
        assert application.state == ApplicationState.PENDING_APPROVAL
        assert len(application.revision_history) == 2
        assert application.revision_history[1]["feedback"] == "Make it shorter."
    finally:
        db.close()


def test_drafting_refuses_empty_research_with_a_readable_error():
    """Without site text the model answers with a request for information
    instead of JSON, which surfaced as a confusing "invalid JSON" failure."""
    db = SessionLocal()
    try:
        company = Company(
            name="Blank Co",
            website_url="https://blank-draft.example",
            research={"pages": [{"url": "https://blank-draft.example", "text": ""}], "empty": True},
        )
        db.add(company)
        db.commit()

        with pytest.raises(ValueError, match="came back empty"):
            create_draft_application(db, FakeLLMProvider("{}"), company)
    finally:
        db.close()
