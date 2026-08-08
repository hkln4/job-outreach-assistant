import json

import app.applications.review as review
import app.companies.workflow as workflow
from app.db import SessionLocal, init_db
from app.main import app
from app.models import CV, Application, Company, Preference
from app.web.deps import get_llm
from tests.fakes import SequencedLLMProvider

from fastapi.testclient import TestClient

client = TestClient(app)


def setup_module():
    init_db()


def teardown_module():
    app.dependency_overrides.pop(get_llm, None)


def test_full_workflow_add_preference_company_research_draft_approve(monkeypatch):
    # TestClient follows redirects by default, so successful POSTs land on 200
    # (the redirected-to page), not the raw 303.

    # 1. CV variant first - the onboarding gate (main.py) redirects every other
    # page to /cvs until at least one CV exists, and a file is required.
    r = client.post(
        "/cvs",
        data={"name": "Backend CV", "target_role": "Backend Engineer", "notes": ""},
        files={"file": ("backend_cv.pdf", b"%PDF-1.4 fake cv content", "application/pdf")},
    )
    assert r.status_code == 200
    assert "Backend CV" in r.text

    # 2. Preference
    r = client.post("/preferences", data={"description": "Avoid defense industry", "type": "hard", "signals": "defense"})
    assert r.status_code == 200
    assert "Avoid defense industry" in r.text

    # 3. Seed the Profile directly via the review/edit endpoint (valid UI path,
    # skips needing a real LLM call for synthesis in this test)
    r = client.post(
        "/profile/update",
        data={
            "summary": "Backend engineer with Python experience.",
            "target_roles": "Backend Engineer",
            "skills": "Python | cv.pdf",
        },
    )
    assert r.status_code == 200

    # 4. Company
    r = client.post(
        "/companies",
        data={"name": "Widgets Inc", "website_url": "https://widgets.example", "notes": ""},
    )
    assert r.status_code == 200
    assert "Widgets Inc" in r.text

    db = SessionLocal()
    company = db.query(Company).filter(Company.name == "Widgets Inc").first()
    company.contact_email = "jobs@widgets.example"
    db.commit()
    company_id = company.id
    db.close()

    # Company detail page renders
    r = client.get(f"/companies/{company_id}")
    assert r.status_code == 200
    assert "Widgets Inc" in r.text

    # 5. Research - avoid a real network fetch, use canned pages
    monkeypatch.setattr(
        workflow,
        "fetch_company_research",
        lambda website_url, careers_url=None: {
            "pages": [{"url": website_url, "text": "We build widgets. No defense involvement."}],
            "detected_careers_url": None,
        },
    )
    fake_llm = SequencedLLMProvider(
        [
            json.dumps({"channels": []}),  # channel detection
            json.dumps({"flags": []}),  # preference check - no conflict this time
            json.dumps(
                {
                    "target_position": "Backend Engineer",
                    "cv_choice_name": "Backend CV",
                    "reasoning": "Good fit.",
                    "subject": "Application for Backend Engineer",
                    "cover_letter_body": "Dear Widgets Inc, ...",
                    "sources": ["https://widgets.example"],
                }
            ),  # draft
        ]
    )
    app.dependency_overrides[get_llm] = lambda: fake_llm

    r = client.post(f"/companies/{company_id}/research", follow_redirects=True)
    assert r.status_code == 200
    assert "widgets.example" in r.text

    # 6. Draft application
    r = client.post(f"/companies/{company_id}/draft", follow_redirects=False)
    assert r.status_code == 303
    application_location = r.headers["location"]
    assert application_location.startswith("/applications/")

    r = client.get(application_location)
    assert r.status_code == 200
    assert "Dear Widgets Inc" in r.text
    assert "PENDING_APPROVAL" in r.text

    db = SessionLocal()
    application = db.query(Application).filter(Application.company_id == company_id).first()
    application_id = application.id
    db.close()

    # 7. Approve & send - mock the actual Gmail send
    monkeypatch.setattr(
        review, "send_email", lambda to, subject, body_text, attachment_path=None: {"id": "m1", "threadId": "t1"}
    )
    r = client.post(f"/applications/{application_id}/approve", follow_redirects=True)
    assert r.status_code == 200
    assert "AWAITING_REPLY" in r.text
