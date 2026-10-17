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
        review, "send_email", lambda to, subject, body_text, attachment_path=None, attachment_name=None: {"id": "m1", "threadId": "t1"}
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
        review, "send_email", lambda to, subject, body_text, attachment_path=None, attachment_name=None: {"id": "m2", "threadId": "t2"}
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
            assert False, "expected DuplicateSendError"
        except review.DuplicateSendError as exc:
            assert exc.prior.id == application.id

        # the owner can still explicitly override after seeing the warning
        result = review.approve_and_send(db, second_application, company.contact_email, allow_duplicate=True)
        assert result.gmail_thread_id == "t2"
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


def _fake_send(calls):
    def send(to, subject, body_text, attachment_path=None, attachment_name=None):
        calls.append(to)
        return {"id": f"m{len(calls)}", "threadId": f"t{len(calls)}"}

    return send


def test_an_application_is_never_sent_twice(monkeypatch):
    calls = []
    monkeypatch.setattr(review, "send_email", _fake_send(calls))

    db = SessionLocal()
    try:
        company, application = _seed_company_and_application(db, contact_email="twice@acme.example")
        review.approve_and_send(db, application, company.contact_email)

        # Neither a repeat approve nor the duplicate override sends it again.
        for allow_duplicate in (False, True):
            try:
                review.approve_and_send(db, application, company.contact_email, allow_duplicate=allow_duplicate)
                assert False, "expected AlreadySentError"
            except review.AlreadySentError:
                pass
        assert len(calls) == 1
    finally:
        db.close()


def test_hand_edited_state_does_not_rearm_a_sent_application(monkeypatch):
    """Sent-ness is read from sent_at / the Gmail ids too, not only the state."""
    calls = []
    monkeypatch.setattr(review, "send_email", _fake_send(calls))

    db = SessionLocal()
    try:
        company, application = _seed_company_and_application(db, contact_email="rearm@acme.example")
        review.approve_and_send(db, application, company.contact_email)
        application.state = ApplicationState.PENDING_APPROVAL  # as an old state override could
        db.commit()

        try:
            review.approve_and_send(db, application, company.contact_email)
            assert False, "expected AlreadySentError"
        except review.AlreadySentError:
            pass
        assert len(calls) == 1
    finally:
        db.close()


def test_failed_gmail_send_releases_the_claim(monkeypatch):
    def failing_send(to, subject, body_text, attachment_path=None, attachment_name=None):
        raise RuntimeError("gmail down")

    monkeypatch.setattr(review, "send_email", failing_send)

    db = SessionLocal()
    try:
        company, application = _seed_company_and_application(db, contact_email="down@acme.example")
        try:
            review.approve_and_send(db, application, company.contact_email)
            assert False, "expected the Gmail error to propagate"
        except RuntimeError:
            pass
        db.refresh(application)
        assert application.state == ApplicationState.PENDING_APPROVAL
        assert application.sent_at is None
    finally:
        db.close()


def test_only_pending_applications_can_be_sent(monkeypatch):
    calls = []
    monkeypatch.setattr(review, "send_email", _fake_send(calls))

    db = SessionLocal()
    try:
        company, application = _seed_company_and_application(db, contact_email="draft@acme.example")
        application.state = ApplicationState.DRAFT
        db.commit()
        try:
            review.approve_and_send(db, application, company.contact_email)
            assert False, "expected ValueError"
        except ValueError as exc:
            assert "not awaiting approval" in str(exc)
        assert calls == []
    finally:
        db.close()


def test_manual_state_changes_follow_the_lifecycle(monkeypatch):
    monkeypatch.setattr(review, "send_email", _fake_send([]))

    db = SessionLocal()
    try:
        company, application = _seed_company_and_application(db, contact_email="states@acme.example")

        # Unsent: can't be marked as sent without sending.
        try:
            review.set_application_state(db, application, ApplicationState.REPLIED)
            assert False, "expected StateTransitionError"
        except review.StateTransitionError:
            pass

        review.approve_and_send(db, application, company.contact_email)
        review.set_application_state(db, application, ApplicationState.INTERVIEW)
        assert application.state == ApplicationState.INTERVIEW

        # Sent: never back to a sendable state.
        for target in (ApplicationState.PENDING_APPROVAL, ApplicationState.DRAFT):
            try:
                review.set_application_state(db, application, target)
                assert False, "expected StateTransitionError"
            except review.StateTransitionError:
                pass
        assert application.state == ApplicationState.INTERVIEW
    finally:
        db.close()


def test_attachment_goes_out_under_the_cv_display_name(monkeypatch):
    seen = {}

    def send(to, subject, body_text, attachment_path=None, attachment_name=None):
        seen.update(path=attachment_path, name=attachment_name)
        return {"id": "m-att", "threadId": "t-att"}

    monkeypatch.setattr(review, "send_email", send)
    db = SessionLocal()
    try:
        company, application = _seed_company_and_application(db, contact_email="att@acme.example")
        review.approve_and_send(db, application, company.contact_email)
        assert seen == {"path": "data/cv_variants/backend.pdf", "name": "Backend_CV.pdf"}
    finally:
        db.close()
