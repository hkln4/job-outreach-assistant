import json

from app.applications.drafting import create_draft_application, regenerate_draft
from app.db import SessionLocal, init_db
from app.models import CV, ApplicationState, Company, Profile
from tests.fakes import FakeLLMProvider

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
        research={"pages": [{"url": "https://acme.example/careers", "text": "We use Python and Postgres."}]},
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
