"""Letter language chosen on the Preferences page reaches the drafting prompt
and the reviewer pass; "no preference" leaves the prompt's own fallback
(template language, else Turkish) in charge."""
import json

import pytest
from fastapi.testclient import TestClient

from app.applications.draft_review import REVIEW_SYSTEM_PROMPT
from app.applications.drafting import create_draft_application
from app.config import get_settings
from app.db import SessionLocal, init_db
from app.main import app
from app.models import CV, Company, OwnerSettings, Profile, get_email_language
from tests.fakes import FakeLLMProvider

client = TestClient(app)

PAGE = (
    "We build route optimisation software for logistics companies in Ankara, written in Python and Go. "
    "We are hiring a junior backend engineer to work on our planning API and data pipelines. "
    "Our team ships weekly and writes about its work on the company blog."
)
DRAFT = json.dumps(
    {
        "target_position": "Backend Engineer",
        "cv_choice_name": "Language CV",
        "reasoning": "Fits.",
        "subject": "Junior Backend Engineer Application",
        "cover_letter_body": "Hello, I built a routing service in Go.",
        "sources": ["https://lang.example"],
        "evidence_item_ids": [],
        "unsupported_claims": [],
    }
)


def setup_module():
    init_db()
    db = SessionLocal()
    try:
        if db.query(CV).first() is None:  # onboarding gate redirects everywhere without one
            db.add(CV(name="Language CV", file_path="data/cv_variants/language.pdf"))
            db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def reset_language():
    yield
    db = SessionLocal()
    try:
        db.query(OwnerSettings).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture
def company():
    db = SessionLocal()
    try:
        if db.query(Profile).first() is None:
            db.add(Profile(summary="Backend engineer.", skills=[], target_roles=["Backend Engineer"]))
        company = Company(
            name="Lang Co",
            website_url="https://lang.example",
            research={"pages": [{"url": "https://lang.example", "text": PAGE}], "summary": "Routing software."},
        )
        db.add(company)
        db.commit()
        yield db, company
    finally:
        db.close()


def _language():
    db = SessionLocal()
    try:
        return get_email_language(db)
    finally:
        db.close()


def test_language_is_saved_from_the_preferences_page():
    assert "Letter language" in client.get("/preferences").text

    client.post("/preferences/email-language", data={"email_language": "en"})
    assert _language() == "en"
    assert 'value="en" selected' in client.get("/preferences").text

    client.post("/preferences/email-language", data={"email_language": ""})
    assert _language() == ""


def test_unknown_language_falls_back_to_no_preference():
    client.post("/preferences/email-language", data={"email_language": "de"})
    assert _language() == ""


def _draft_prompt(llm):
    # the plain-text letter call (roadmap phase 4: angles -> letter -> metadata)
    return next(c["prompt"] for c in llm.calls if "Output plain text only" in (c["system"] or ""))


def test_chosen_language_reaches_the_draft_and_the_reviewer(company, monkeypatch):
    db, company = company
    client.post("/preferences/email-language", data={"email_language": "en"})
    monkeypatch.setattr(get_settings(), "draft_review_enabled", True)
    drafter, reviewer = FakeLLMProvider(DRAFT), FakeLLMProvider(json.dumps({"issues": [], "overall": "Ready."}))

    create_draft_application(db, drafter, company, review_llm=reviewer)

    assert "OWNER'S PREFERRED EMAIL LANGUAGE:\nEnglish" in _draft_prompt(drafter)
    (review_call,) = [c for c in reviewer.calls if c["system"] == REVIEW_SYSTEM_PROMPT]
    assert "OWNER'S PREFERRED EMAIL LANGUAGE:\nEnglish" in review_call["prompt"]


def test_no_preference_leaves_the_prompt_fallback(company):
    db, company = company
    drafter = FakeLLMProvider(DRAFT)

    create_draft_application(db, drafter, company)

    assert "OWNER'S PREFERRED EMAIL LANGUAGE:" not in _draft_prompt(drafter)
