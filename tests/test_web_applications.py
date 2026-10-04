"""Discarding a draft that shouldn't have been started, and the guard that
stops an unsaved edit from being silently dropped at send time."""
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.db import SessionLocal, init_db
from app.main import app
from app.models import (
    CV,
    Application,
    ApplicationState,
    Company,
    Contact,
    ContactState,
    FollowUp,
)

client = TestClient(app)


def setup_module():
    init_db()
    db = SessionLocal()
    try:
        if db.query(CV).first() is None:  # onboarding gate redirects everywhere without one
            db.add(CV(name="Test Applications CV", file_path="data/cv_variants/test_applications.pdf"))
            db.commit()
    finally:
        db.close()


def _seed(state=ApplicationState.PENDING_APPROVAL, sent=False, name="Discard Co") -> tuple[int, int]:
    db = SessionLocal()
    try:
        company = Company(name=name, website_url=f"https://{name.replace(' ', '').lower()}.example")
        db.add(company)
        db.commit()
        application = Application(
            company_id=company.id,
            target_position="Backend Engineer",
            subject="Application",
            cover_letter_body="Dear team, ...",
            state=state,
            sent_at=datetime.now(timezone.utc) if sent else None,
        )
        db.add(application)
        db.commit()
        return company.id, application.id
    finally:
        db.close()


def test_draft_can_be_discarded_and_frees_the_company():
    company_id, application_id = _seed()

    r = client.post(f"/applications/{application_id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/companies/{company_id}"

    db = SessionLocal()
    try:
        assert db.get(Application, application_id) is None
    finally:
        db.close()


def test_sent_application_is_never_discarded():
    """Past applications are retained with their final state (CLAUDE.md 5.6)."""
    _, application_id = _seed(state=ApplicationState.AWAITING_REPLY, sent=True, name="Sent Co")

    client.post(f"/applications/{application_id}/delete", follow_redirects=False)

    db = SessionLocal()
    try:
        assert db.get(Application, application_id) is not None
    finally:
        db.close()


def test_discarding_an_escalation_draft_releases_its_contact():
    """Contact.application_id has no ORM relationship, so nothing would clear
    it automatically - the contact would sit in DRAFT forever."""
    company_id, application_id = _seed(name="Escalation Discard Co")
    db = SessionLocal()
    try:
        contact = Contact(
            company_id=company_id,
            name="Ada Lovelace",
            email="ada@escalationdiscard.example",
            state=ContactState.DRAFT,
            application_id=application_id,
        )
        db.add(contact)
        db.add(FollowUp(application_id=application_id, body="ping"))
        db.commit()
        contact_id = contact.id
    finally:
        db.close()

    client.post(f"/applications/{application_id}/delete", follow_redirects=False)

    db = SessionLocal()
    try:
        contact = db.get(Contact, contact_id)
        assert contact.application_id is None
        assert contact.state == ContactState.NOT_CONTACTED
        assert db.query(FollowUp).filter(FollowUp.application_id == application_id).count() == 0
    finally:
        db.close()


def test_discard_button_only_shows_before_sending():
    _, draft_id = _seed(name="Draft Button Co")
    _, sent_id = _seed(state=ApplicationState.AWAITING_REPLY, sent=True, name="Sent Button Co")

    assert f"/applications/{draft_id}/delete" in client.get(f"/applications/{draft_id}").text
    assert f"/applications/{sent_id}/delete" not in client.get(f"/applications/{sent_id}").text


def test_unsaved_edit_guard_is_wired_into_the_review_page():
    """The guard is client-side, so what the server test can assert is that the
    page ships it: the script, its message, and the badge it toggles."""
    _, draft_id = _seed(name="Guard Co")
    page = client.get(f"/applications/{draft_id}").text

    assert "/static/draft_guard.js" in page
    assert 'id="draft-guard-i18n"' in page
    assert 'id="unsaved-badge"' in page
    assert "review-approve-form" in page  # what the guard hooks


def test_sent_application_does_not_ship_the_guard():
    _, sent_id = _seed(state=ApplicationState.AWAITING_REPLY, sent=True, name="No Guard Co")
    page = client.get(f"/applications/{sent_id}").text

    assert "/static/draft_guard.js" not in page
