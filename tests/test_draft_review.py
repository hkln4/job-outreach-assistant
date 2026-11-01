"""Independent reviewer pass on drafts (app/applications/draft_review.py): its
issues are stored with the current draft's notes and shown in "check before
sending", quotes not in the draft are dropped, it never blocks a draft, and
DRAFT_REVIEW_ENABLED=false skips the call."""
import json

import pytest
from fastapi.testclient import TestClient

from app.applications.draft_review import REVIEW_SYSTEM_PROMPT, quote_in_draft, review_feedback
from app.applications.drafting import create_draft_application, regenerate_draft
from app.config import get_settings
from app.db import SessionLocal, init_db
from app.main import app
from app.models import CV, Application, Company, Profile
from app.web.deps import get_draft_llm, get_review_llm
from tests.fakes import FakeLLMProvider

client = TestClient(app)

PAGE = (
    "We build computer vision systems for factories in Ankara: defect detection on production lines, "
    "written in Python with PyTorch, deployed on edge devices. We are hiring a junior ML engineer. "
    "The team publishes case studies about the inspection systems it has delivered to customers."
)
BODY = (
    "Merhaba, I am a new graduate applying for the ML Engineer role. "
    "I have always admired your innovative culture. I built a defect detector in PyTorch."
)
DRAFT = json.dumps(
    {
        "target_position": "ML Engineer",
        "cv_choice_name": "Review CV",
        "reasoning": "Fits.",
        "subject": "Junior ML Engineer Application",
        "cover_letter_body": BODY,
        "sources": ["https://review.example"],
        "evidence_item_ids": [],
        "unsupported_claims": [],
    }
)
REVIEW = json.dumps(
    {
        "issues": [
            {"kind": "generic_phrase", "quote": "I have always admired your innovative culture.",
             "suggestion": "Name what the company builds instead."},
            {"kind": "wrong_language", "quote": "Merhaba,", "suggestion": "Use one language."},
            {"kind": "unsupported_claim", "quote": "I led a team of ten engineers.",
             "suggestion": "Remove it."},  # not in the draft - must be dropped
            {"kind": "made_up_kind", "quote": "defect detector", "suggestion": "Say what it detected."},
        ],
        "overall": "Close, but fix the generic motivation line.",
    }
)


def setup_module():
    init_db()


@pytest.fixture
def company():
    db = SessionLocal()
    try:
        if db.query(Profile).first() is None:
            db.add(Profile(summary="ML engineer.", skills=[], target_roles=["ML Engineer"]))
        if db.query(CV).filter(CV.name == "Review CV").first() is None:
            db.add(CV(name="Review CV", target_role="ML Engineer", file_path="data/cv_variants/review.pdf"))
        company = Company(
            name="Review Co",
            website_url="https://review.example",
            contact_email="jobs@review.example",
            research={"pages": [{"url": "https://review.example", "text": PAGE}], "summary": "Factory CV systems."},
        )
        db.add(company)
        db.commit()
        yield db, company
    finally:
        db.close()


@pytest.fixture
def review_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "draft_review_enabled", True)


def _review_calls(llm):
    return [c for c in llm.calls if c["system"] == REVIEW_SYSTEM_PROMPT]


def test_review_is_stored_and_fabricated_quotes_are_dropped(company, review_on):
    db, company = company
    reviewer = FakeLLMProvider(REVIEW)

    application = create_draft_application(db, FakeLLMProvider(DRAFT), company, review_llm=reviewer)

    review = application.revision_history[0]["review"]
    assert [i["kind"] for i in review["issues"]] == ["generic_phrase", "wrong_language", "other"]
    assert review["dropped_quotes"] == 1
    assert review["overall"].startswith("Close")
    (call,) = _review_calls(reviewer)
    assert BODY in call["prompt"]
    assert "<<<UNTRUSTED COMPANY CONTENT>>>" in call["prompt"]  # company text is fenced
    assert call["json_schema"] is not None


def test_setting_off_skips_the_call(company):
    db, company = company
    reviewer = FakeLLMProvider(REVIEW)

    application = create_draft_application(db, FakeLLMProvider(DRAFT), company, review_llm=reviewer)

    assert reviewer.calls == []
    assert "review" not in application.revision_history[0]


def test_a_failed_review_never_blocks_the_draft(company, review_on):
    db, company = company

    application = create_draft_application(db, FakeLLMProvider(DRAFT), company, review_llm=FakeLLMProvider("nope"))

    assert application.cover_letter_body == BODY
    assert "error" in application.revision_history[0]["review"]


def test_regenerate_replaces_the_old_review(company, review_on):
    db, company = company
    application = create_draft_application(db, FakeLLMProvider(DRAFT), company, review_llm=FakeLLMProvider(REVIEW))

    clean = json.dumps({"issues": [], "overall": "Ready to send."})
    regenerate_draft(db, FakeLLMProvider(DRAFT), application, "shorter", review_llm=FakeLLMProvider(clean))

    assert application.revision_history[0]["review"] == {"issues": [], "overall": "Ready to send.", "dropped_quotes": 0}
    assert application.revision_history[-1]["feedback"] == "shorter"


def test_review_page_shows_issues_and_rewrite_sends_them_as_feedback(company, review_on):
    db, company = company
    application = create_draft_application(db, FakeLLMProvider(DRAFT), company, review_llm=FakeLLMProvider(REVIEW))
    drafter = FakeLLMProvider(DRAFT)
    app.dependency_overrides[get_draft_llm] = lambda: drafter
    app.dependency_overrides[get_review_llm] = lambda: FakeLLMProvider(json.dumps({"issues": [], "overall": "Ready."}))
    try:
        page = client.get(f"/applications/{application.id}").text
        assert "Reviewer notes" in page
        assert "Name what the company builds instead." in page
        assert "I led a team of ten engineers." not in page
        assert "Rewrite with these notes" in page

        feedback = review_feedback(application.revision_history[0]["review"])
        r = client.post(f"/applications/{application.id}/reject", data={"feedback": feedback}, follow_redirects=False)
        assert r.status_code == 303
    finally:
        app.dependency_overrides.pop(get_draft_llm, None)
        app.dependency_overrides.pop(get_review_llm, None)

    assert "Name what the company builds instead." in drafter.calls[-1]["prompt"]  # went through regenerate
    db.expire_all()
    refreshed = db.get(Application, application.id)
    assert refreshed.revision_history[0]["review"]["overall"] == "Ready."


@pytest.mark.parametrize(
    "quote, expected",
    [
        ("I  have always\nadmired", True),  # whitespace differences are fine
        ("i have ALWAYS admired", True),
        ("“Merhaba,”", True),  # surrounding quotes stripped
        ("Junior ML Engineer", True),  # subject counts too
        ("I", False),  # too short to mean anything
        ("I admired your culture", False),
    ],
)
def test_quote_matching(quote, expected):
    assert quote_in_draft(quote, "Junior ML Engineer Application", BODY) is expected


def test_no_issues_means_no_feedback():
    assert review_feedback({"issues": []}) == ""
    assert review_feedback(None) == ""
