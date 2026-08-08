import app.applications.review as review
from app.db import SessionLocal, init_db
from app.models import CV, Application, ApplicationState, Company


def setup_module():
    init_db()


def _seed_company_and_application(db, contact_email="jobs@acme.example"):
    cv = CV(name="Backend CV", file_path="data/cv_variants/backend.pdf")
    company = Company(name="Acme", website_url="https://acme.example", contact_email=contact_email)
    db.add_all([cv, company])
    db.commit()
    db.refresh(company)
    db.refresh(cv)

    application = Application(
        company_id=company.id,
        cv_id=cv.id,
        subject="Application for Backend Engineer",
        cover_letter_body="Dear Acme, ...",
        state=ApplicationState.PENDING_APPROVAL,
    )
    db.add(application)
    db.commit()
    db.refresh(application)
    return company, application


def test_approve_and_send_transitions_to_awaiting_reply(monkeypatch):
    monkeypatch.setattr(
        review, "send_email", lambda to, subject, body_text, attachment_path=None: {"id": "m1", "threadId": "t1"}
    )

    db = SessionLocal()
    try:
        company, application = _seed_company_and_application(db)
        result = review.approve_and_send(db, application, company.contact_email)

        assert result.state == ApplicationState.AWAITING_REPLY
        assert result.gmail_message_id == "m1"
        assert result.gmail_thread_id == "t1"
        assert result.sent_at is not None
    finally:
        db.close()


def test_approve_and_send_blocks_duplicate_recipient(monkeypatch):
    monkeypatch.setattr(
        review, "send_email", lambda to, subject, body_text, attachment_path=None: {"id": "m2", "threadId": "t2"}
    )

    db = SessionLocal()
    try:
        company, application = _seed_company_and_application(db, contact_email="dup@acme.example")
        review.approve_and_send(db, application, company.contact_email)

        # a second application to the same company/address should be blocked
        second_application = Application(
            company_id=company.id,
            subject="Second try",
            cover_letter_body="...",
            state=ApplicationState.PENDING_APPROVAL,
        )
        db.add(second_application)
        db.commit()
        db.refresh(second_application)

        try:
            review.approve_and_send(db, second_application, company.contact_email)
            assert False, "expected dedup ValueError"
        except ValueError as exc:
            assert "dedup" in str(exc)
    finally:
        db.close()


def test_approve_and_send_rejects_invalid_email():
    db = SessionLocal()
    try:
        _, application = _seed_company_and_application(db, contact_email="not-an-email")
        try:
            review.approve_and_send(db, application, "not-an-email")
            assert False, "expected invalid-address ValueError"
        except ValueError as exc:
            assert "Invalid recipient" in str(exc)
    finally:
        db.close()
