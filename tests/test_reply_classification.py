"""Bounces and auto-replies are not replies (phase 0 bug 3), a Gmail timeout
after acceptance is never resent (bug 6), and the exact CV file sent is
recorded (bug 7)."""
from datetime import datetime, timedelta, timezone

import app.applications.reply_check as reply_check
import app.applications.review as review
from app.db import SessionLocal, init_db
from app.email_send.gmail_client import classify_message, classify_thread_messages
from app.models import CV, Application, ApplicationState, Company
from tests.conftest import make_cv_file

OWNER = "Owner.Name@Gmail.com"


def setup_module():
    init_db()


def _msg(msg_id: str, labels=None, **headers) -> dict:
    return {
        "id": msg_id,
        "labelIds": labels or ["INBOX"],
        "payload": {"headers": [{"name": k.replace("_", "-"), "value": v} for k, v in headers.items()]},
    }


def test_own_message_is_matched_by_parsed_address_case_insensitively():
    assert classify_message({"From": "Owner Name <owner.name@gmail.com>"}, OWNER)[0] == "own"
    # Gmail's SENT label marks our own message even with no address configured
    assert classify_message({"From": "x@y.example"}, "", ["SENT"])[0] == "own"
    # a substring of the owner's address is not the owner
    assert classify_message({"From": "notowner.name@gmail.com.evil.example"}, OWNER)[0] == "reply"


def test_bounce_out_of_office_and_real_reply_are_told_apart():
    bounce = _msg("b1", From="Mail Delivery Subsystem <MAILER-DAEMON@googlemail.com>", Subject="Delivery Status Notification (Failure)")
    report = _msg("b2", From="postmaster@acme.example", Content_Type='multipart/report; report-type=delivery-status; boundary="x"')
    ooo = _msg("a1", From="ik@acme.example", Subject="Ofis dışında: Başvurunuz", Auto_Submitted="auto-replied")
    ooo_subject_only = _msg("a2", From="hr@acme.example", Subject="Automatic reply: Application")
    reply = _msg("r1", From="Ayşe <ayse@acme.example>", Subject="Re: Application")
    sent = _msg("s1", ["SENT"], From=OWNER, Subject="Application")

    assert classify_thread_messages([sent, bounce], OWNER)["kind"] == "bounce"
    assert classify_thread_messages([sent, report], OWNER)["kind"] == "bounce"
    assert classify_thread_messages([sent, ooo], OWNER)["kind"] == "auto_reply"
    assert classify_thread_messages([sent, ooo_subject_only], OWNER)["kind"] == "auto_reply"
    assert classify_thread_messages([sent, ooo, reply], OWNER)["kind"] == "reply"
    assert classify_thread_messages([sent], OWNER)["kind"] == "none"
    assert classify_message({"From": "a@b.example", "Precedence": "bulk"})[0] == "auto_reply"
    assert classify_message({"From": "a@b.example", "Auto-Submitted": "no"})[0] == "reply"


def _sent_application(db, name: str, sent_to: str) -> Application:
    company = Company(
        name=name,
        website_url=f"https://{name.lower()}.example",
        contact_email=sent_to,
        research={"emails": [{"email": sent_to, "kind": "hr"}, {"email": f"info@{name.lower()}.example", "kind": "general", "context": "footer"}]},
    )
    db.add(company)
    db.commit()
    application = Application(
        company_id=company.id,
        subject="Application",
        cover_letter_body="Body",
        state=ApplicationState.AWAITING_REPLY,
        sent_at=datetime.now(timezone.utc) - timedelta(days=2),
        sent_to=sent_to,
        gmail_message_id="m",
        gmail_thread_id=f"t-{name}",
    )
    db.add(application)
    db.commit()
    db.refresh(application)
    return application


def test_bounce_marks_the_address_and_offers_the_next_one(monkeypatch):
    monkeypatch.setattr(reply_check, "classify_thread", lambda t: {"kind": "bounce", "detail": "hr@bounceco.example", "message_id": "b"})
    db = SessionLocal()
    try:
        application = _sent_application(db, "BounceCo", "hr@bounceco.example")
        assert reply_check.check_application_reply(db, application) == "bounce"
        company = application.company
        assert application.state == ApplicationState.BOUNCED
        assert company.invalid_emails == ["hr@bounceco.example"]
        assert company.contact_email is None
        assert [e["email"] for e in reply_check.alternative_emails(application)] == ["info@bounceco.example"]

        copy = reply_check.resend_after_bounce(db, application, "info@bounceco.example")
        assert copy.state == ApplicationState.PENDING_APPROVAL and copy.cover_letter_body == "Body"
        assert company.contact_email == "info@bounceco.example"
        # the bounce never reached the company, so dedup doesn't block the copy
        assert review.find_prior_send(db, company.id, "info@bounceco.example", exclude_application_id=copy.id) is None
    finally:
        db.close()


def test_auto_reply_is_a_note_and_possible_replies_are_only_suggested(monkeypatch):
    monkeypatch.setattr(reply_check, "classify_thread", lambda t: {"kind": "auto_reply", "detail": "Out of Office", "message_id": "a"})
    monkeypatch.setattr(
        reply_check,
        "possible_replies",
        lambda domain, after, exclude: [{"id": "x1", "threadId": "other", "from": f"cto@{domain}", "subject": "Your email", "date": "today"}],
    )
    db = SessionLocal()
    try:
        application = _sent_application(db, "AutoCo", "hr@autoco.example")
        assert reply_check.check_application_reply(db, application) == "auto_reply"
        assert application.state == ApplicationState.AWAITING_REPLY
        kinds = [n["kind"] for n in application.notes]
        assert kinds == ["auto_reply", "possible_reply"]
        assert "cto@autoco.example" in application.notes[1]["detail"]
        # checking again doesn't duplicate notes
        reply_check.check_application_reply(db, application)
        assert len(application.notes) == 2
    finally:
        db.close()


def _pending(db, name: str) -> Application:
    cv = CV(name=f"{name} CV", file_path=make_cv_file(f"{name}.pdf", name.encode()))
    company = Company(name=name, contact_email=f"jobs@{name.lower()}.example")
    db.add_all([cv, company])
    db.commit()
    application = Application(company_id=company.id, cv_id=cv.id, subject="S", cover_letter_body="B", state=ApplicationState.PENDING_APPROVAL)
    db.add(application)
    db.commit()
    db.refresh(application)
    return application


def test_timeout_after_gmail_accepted_is_recorded_not_resent(monkeypatch):
    seen = {}

    def timing_out_send(**kwargs):
        seen.update(kwargs)
        raise TimeoutError("read timed out")

    monkeypatch.setattr(review, "send_email", timing_out_send)
    monkeypatch.setattr(review, "find_sent_message", lambda mid, to, subject, after: {"id": "found", "threadId": "tf"} if mid == seen["message_id"] else None)
    db = SessionLocal()
    try:
        application = _pending(db, "TimeoutCo")
        review.approve_and_send(db, application, "jobs@timeoutco.example")
        assert application.state == ApplicationState.AWAITING_REPLY
        assert application.gmail_message_id == "found"
        assert application.rfc_message_id == seen["message_id"]
        assert application.notes[0]["kind"] == "send_recovered"
    finally:
        db.close()


def test_failed_send_not_found_in_sent_is_released(monkeypatch):
    def failing(**kwargs):
        raise RuntimeError("gmail down")

    monkeypatch.setattr(review, "send_email", failing)
    monkeypatch.setattr(review, "find_sent_message", lambda *a: None)
    db = SessionLocal()
    try:
        application = _pending(db, "DownCo")
        try:
            review.approve_and_send(db, application, "jobs@downco.example")
            raise AssertionError("expected the Gmail error")
        except RuntimeError:
            pass
        assert application.state == ApplicationState.PENDING_APPROVAL
        assert application.rfc_message_id is None
    finally:
        db.close()


def test_cv_snapshot_survives_the_variant_being_replaced(monkeypatch):
    monkeypatch.setattr(review, "send_email", lambda **kw: {"id": "m", "threadId": "t"})
    db = SessionLocal()
    try:
        application = _pending(db, "SnapCo")
        review.approve_and_send(db, application, "jobs@snapco.example")
        snapshot = application.cv_snapshot
        with open(application.cv.file_path, "wb") as f:  # owner uploads a new version
            f.write(b"a different CV")
        with open(snapshot["path"], "rb") as f:
            assert f.read() == b"SnapCo"
        assert snapshot["name"] == "SnapCo_CV.pdf"
    finally:
        db.close()


def test_missing_cv_file_stops_the_send_before_the_claim(monkeypatch):
    monkeypatch.setattr(review, "send_email", lambda **kw: {"id": "m", "threadId": "t"})
    db = SessionLocal()
    try:
        application = _pending(db, "MissingCo")
        application.cv.file_path = "/nonexistent/cv.pdf"
        db.commit()
        try:
            review.approve_and_send(db, application, "jobs@missingco.example")
            raise AssertionError("expected a missing-file error")
        except ValueError as exc:
            assert "missing" in str(exc)
        assert application.state == ApplicationState.PENDING_APPROVAL
    finally:
        db.close()


def test_bounced_application_page_offers_the_other_addresses(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(reply_check, "classify_thread", lambda t: {"kind": "bounce", "detail": "", "message_id": "b"})
    db = SessionLocal()
    try:
        if db.query(CV).first() is None:
            db.add(CV(name="Gate CV", file_path=""))
        application = _sent_application(db, "PageBounceCo", "hr@pagebounceco.example")
        application.last_checked_at = datetime.now(timezone.utc)  # skip the page-load auto-check
        reply_check.mark_bounced(db, application)
        db.commit()
        application_id = application.id
    finally:
        db.close()
    page = TestClient(app).get(f"/applications/{application_id}")
    assert page.status_code == 200
    assert "info@pagebounceco.example" in page.text
    response = TestClient(app).post(f"/applications/{application_id}/resend", data={"to": "info@pagebounceco.example"})
    assert response.status_code == 200 and "Pending approval" in response.text
