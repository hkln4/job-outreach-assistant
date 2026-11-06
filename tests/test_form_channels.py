"""Roadmap phase 6: a form application is marked submitted by the owner (the
app never submits), tracked by the company's domain, and job pages found on a
company's site become postings in one click."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.applications.reply_check as reply_check
import app.postings.service as posting_service
from app.applications.review import DuplicateSendError, mark_submitted
from app.db import SessionLocal, init_db
from app.main import app
from app.models import CV, Application, ApplicationState, Company, JobPosting
from app.web.deps import get_llm
from tests.conftest import make_cv_file
from tests.fakes import RoutedLLMProvider


def setup_module():
    init_db()
    db = SessionLocal()
    if db.query(CV).first() is None:
        db.add(CV(name="Form Gate CV", file_path=make_cv_file("form-gate.pdf")))
        db.commit()
    db.close()


def test_mark_submitted_tracks_by_domain_and_keeps_dedup(monkeypatch):
    db = SessionLocal()
    try:
        company = Company(name="Form Co", website_url="https://formco.example")
        db.add(company)
        db.commit()
        application = Application(company_id=company.id, subject="S", cover_letter_body="B", state=ApplicationState.PENDING_APPROVAL)
        db.add(application)
        db.commit()
        mark_submitted(db, application, "site_form", "https://formco.example/kariyer/basvuru")
        assert application.state == ApplicationState.AWAITING_REPLY and application.channel == "site_form"

        searched = {}
        monkeypatch.setattr(reply_check, "possible_replies", lambda domain, after, thread: searched.update(domain=domain) or [
            {"id": "x", "threadId": "t", "from": "ik@formco.example", "subject": "Başvurunuz", "date": "d"}])
        application.sent_at = datetime.now(timezone.utc) - timedelta(days=1)
        assert reply_check.check_application_reply(db, application) == "none"
        assert searched["domain"] == "formco.example"
        assert application.notes[0]["kind"] == "possible_reply"

        second = Application(company_id=company.id, subject="S", cover_letter_body="B", state=ApplicationState.PENDING_APPROVAL)
        db.add(second)
        db.commit()
        with pytest.raises(DuplicateSendError):
            mark_submitted(db, second, "site_form", "https://formco.example/x")
    finally:
        db.close()


def test_job_page_from_research_becomes_a_posting(monkeypatch):
    text = "Junior ML Engineer\nWe are hiring a junior ML engineer in Ankara. Requirements: Python, PyTorch. " * 3
    monkeypatch.setattr(posting_service, "fetch_posting", lambda url: {
        "url": url, "final_url": url, "status": 200, "html": "", "text": text, "json_ld": {}, "safety": {}, "expired": None})
    parsed = json.dumps({"title": "Junior ML Engineer", "company_name": "", "location": "Ankara", "work_mode": "onsite",
                         "requirements": ["Python, PyTorch"], "nice_to_have": [], "seniority": "junior", "deadline": "", "apply_url": "", "employment_type": "full-time"})
    app.dependency_overrides[get_llm] = lambda: RoutedLLMProvider({"parsing ONE job posting": parsed}, default=json.dumps({"flags": []}))
    db = SessionLocal()
    try:
        company = Company(name="Page Co", website_url="https://pageco.example")
        db.add(company)
        db.commit()
        company_id = company.id
    finally:
        db.close()
    try:
        response = TestClient(app).post(f"/postings/from-page/{company_id}", data={"url": "https://pageco.example/jobs/ml-engineer"})
        assert response.status_code == 200 and "Junior ML Engineer" in response.text
    finally:
        app.dependency_overrides.pop(get_llm, None)
    db = SessionLocal()
    try:
        posting = db.query(JobPosting).filter(JobPosting.url == "https://pageco.example/jobs/ml-engineer").one()
        assert posting.company_id == company_id and posting.source == "careers_page"
    finally:
        db.close()
