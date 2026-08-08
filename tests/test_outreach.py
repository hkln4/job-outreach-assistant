"""Outreach-CRM behaviors: company-based dedup, send-delay gap, NO_RESPONSE
threshold query, follow-ups, reply-check stamping, and open-application drafting."""
import json
from datetime import datetime, timedelta, timezone

import app.applications.follow_ups as follow_ups_module
import app.applications.reply_check as reply_check
import app.applications.review as review
from app.applications.drafting import create_draft_application
from app.db import SessionLocal, init_db
from app.models import CV, Application, ApplicationState, Company, FollowUpState, Profile
from tests.fakes import FakeLLMProvider


def setup_module():
    init_db()


def _seed_sent_application(db, name="OutreachCo", email="jobs@outreach.example", sent_days_ago=0):
    company = Company(name=name, website_url=f"https://{name.lower()}.example", contact_email=email)
    db.add(company)
    db.commit()
    db.refresh(company)
    application = Application(
        company_id=company.id,
        subject="Application",
        cover_letter_body="Dear team, ...",
        target_position="Backend Engineer",
        state=ApplicationState.AWAITING_REPLY,
        sent_at=datetime.now(timezone.utc) - timedelta(days=sent_days_ago),
        gmail_message_id="m-orig",
        gmail_thread_id="t-orig",
    )
    db.add(application)
    db.commit()
    db.refresh(application)
    return company, application


def test_find_prior_send_matches_by_company_even_if_email_changed():
    db = SessionLocal()
    try:
        company, application = _seed_sent_application(db, name="EmailChangedCo", email="old@ec.example")
        company.contact_email = "new@ec.example"  # owner later fixed the address
        db.commit()

        prior = review.find_prior_send(db, company.id, "new@ec.example")
        assert prior is not None and prior.id == application.id
    finally:
        db.close()


def test_seconds_until_send_allowed_enforces_gap(monkeypatch):
    class _Settings:
        send_delay_seconds = 120
        no_response_days = 14

    monkeypatch.setattr(review, "get_settings", lambda: _Settings())
    db = SessionLocal()
    try:
        _seed_sent_application(db, name="DelayCo", email="jobs@delay.example", sent_days_ago=0)
        assert review.seconds_until_send_allowed(db) > 0

        _Settings.send_delay_seconds = 0
        assert review.seconds_until_send_allowed(db) == 0
    finally:
        db.close()


def test_overdue_applications_uses_threshold_fallback(monkeypatch):
    class _Settings:
        send_delay_seconds = 0
        no_response_days = 14

    monkeypatch.setattr(reply_check, "get_settings", lambda: _Settings())
    db = SessionLocal()
    try:
        _, old_app = _seed_sent_application(db, name="OverdueCo", email="jobs@overdue.example", sent_days_ago=20)
        _, fresh_app = _seed_sent_application(db, name="FreshCo", email="jobs@fresh.example", sent_days_ago=1)

        overdue_ids = {a.id for a in reply_check.overdue_applications(db)}
        assert old_app.id in overdue_ids
        assert fresh_app.id not in overdue_ids

        # an explicit future due date takes precedence over the sent_at fallback
        old_app.follow_up_due_at = datetime.now(timezone.utc) + timedelta(days=5)
        db.commit()
        overdue_ids = {a.id for a in reply_check.overdue_applications(db)}
        assert old_app.id not in overdue_ids
    finally:
        db.close()


def test_check_application_reply_stamps_and_transitions(monkeypatch):
    monkeypatch.setattr(reply_check, "thread_has_reply", lambda thread_id: True)
    db = SessionLocal()
    try:
        _, application = _seed_sent_application(db, name="RepliedCo", email="jobs@replied.example")
        assert reply_check.check_application_reply(db, application) is True
        assert application.state == ApplicationState.REPLIED
        assert application.last_checked_at is not None
    finally:
        db.close()


def test_follow_up_draft_and_send_in_same_thread(monkeypatch):
    db = SessionLocal()
    try:
        _, application = _seed_sent_application(db, name="FollowCo", email="jobs@follow.example", sent_days_ago=15)
        llm = FakeLLMProvider(json.dumps({"body": "Just following up on my application."}))

        follow_up = follow_ups_module.draft_follow_up(db, llm, application)
        assert follow_up.state == FollowUpState.PENDING_APPROVAL
        assert "following up" in follow_up.body

        sent_kwargs = {}

        def fake_send_reply(thread_id, prior_gmail_message_id, to, subject, body_text):
            sent_kwargs.update(
                thread_id=thread_id, prior=prior_gmail_message_id, to=to, subject=subject
            )
            return {"id": "m-follow", "threadId": thread_id}

        import app.email_send.gmail_client as gmail_client

        monkeypatch.setattr(gmail_client, "send_reply", fake_send_reply)

        follow_ups_module.send_follow_up(db, follow_up, "jobs@follow.example")
        assert follow_up.state == FollowUpState.SENT
        assert follow_up.gmail_message_id == "m-follow"
        assert sent_kwargs["thread_id"] == "t-orig"
        assert sent_kwargs["prior"] == "m-orig"
        assert application.follow_up_due_at is not None
    finally:
        db.close()


def test_open_application_draft_uses_owner_position_and_reason():
    db = SessionLocal()
    try:
        profile = Profile(summary="Backend engineer.", skills=[], target_roles=["Backend Engineer"])
        cv = CV(name="Main CV", file_path="data/cv_variants/main.pdf")
        company = Company(
            name="OpenAppCo",
            website_url="https://openapp.example",
            why_interested="They do CV work in Python.",
            research={"pages": [{"url": "https://openapp.example", "text": "We build vision systems."}]},
        )
        db.add_all([profile, cv, company])
        db.commit()
        db.refresh(company)

        llm = FakeLLMProvider(
            json.dumps(
                {
                    "target_position": "Something Else",
                    "cv_choice_name": "Main CV",
                    "reasoning": "Fit.",
                    "subject": "Introduction",
                    "cover_letter_body": "Dear OpenAppCo, ...",
                    "sources": ["https://openapp.example"],
                }
            )
        )
        application = create_draft_application(
            db,
            llm,
            company,
            target_position="Computer Vision Engineer",
            application_reason="No open posting - introducing myself.",
        )

        # owner's explicit position wins over the LLM's, and the reason is retained
        assert application.target_position == "Computer Vision Engineer"
        assert application.application_reason == "No open posting - introducing myself."

        prompt = llm.calls[0]["prompt"]
        assert "OWNER'S CHOSEN POSITION" in prompt
        assert "OWNER'S REASON FOR APPLYING" in prompt
        assert "OWNER'S NOTES ON WHY THIS COMPANY" in prompt
    finally:
        db.close()
