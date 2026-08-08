"""Follow-up emails (email-outreach cadence): a short reminder drafted by the
LLM, reviewed by the owner, and sent INSIDE the original Gmail thread. Never
auto-sent. Escalation to an employee contact stays a separate flow."""
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.llm.base import LLMProvider
from app.llm.json_utils import parse_json_object
from app.models import Application, FollowUp, FollowUpState

FOLLOW_UP_SYSTEM_PROMPT = """You are writing a short follow-up email for a job \
application that got no reply. The original email is given below.

Rules:
- 3-6 sentences: politely reference the earlier application (position/date), restate \
interest in one sentence, and invite a response. No pressure, no guilt-tripping.
- Write in the SAME language as the original email.
- Do NOT introduce any new claims about the company or the owner - a follow-up only \
refers back to what was already sent.
- No subject line - the email is sent as a reply in the same thread.
- Output ONLY valid JSON, this exact shape: {"body": string}
"""

FOLLOW_UP_FALLBACK = {"body": ""}


def next_follow_up_due(sent_at: datetime) -> datetime:
    return sent_at + timedelta(days=get_settings().no_response_days)


def draft_follow_up(db: Session, llm: LLMProvider, application: Application) -> FollowUp:
    """Creates (or refreshes) the pending follow-up draft for this application."""
    days_waiting = ""
    if application.sent_at is not None:
        sent = application.sent_at if application.sent_at.tzinfo else application.sent_at.replace(tzinfo=timezone.utc)
        days_waiting = str((datetime.now(timezone.utc) - sent).days)

    prompt = (
        f"COMPANY: {application.company.name}\n"
        f"POSITION APPLIED FOR: {application.target_position or 'general application'}\n"
        f"DAYS SINCE SENT, WITH NO REPLY: {days_waiting or 'unknown'}\n\n"
        f"ORIGINAL SUBJECT: {application.subject}\n\n"
        f"ORIGINAL EMAIL:\n{application.cover_letter_body}"
    )
    response = llm.generate(prompt=prompt, system=FOLLOW_UP_SYSTEM_PROMPT, temperature=0.3, max_output_tokens=1024)
    data = parse_json_object(response.text, fallback=FOLLOW_UP_FALLBACK)
    if "parse_error" in data or not data.get("body"):
        raise ValueError("Follow-up drafting produced invalid or empty output - try again.")

    # Only one pending draft at a time: regenerating replaces it instead of stacking.
    follow_up = (
        db.query(FollowUp)
        .filter(FollowUp.application_id == application.id, FollowUp.state == FollowUpState.PENDING_APPROVAL)
        .first()
    )
    if follow_up is None:
        follow_up = FollowUp(application_id=application.id)
        db.add(follow_up)
    follow_up.body = data["body"]
    db.commit()
    db.refresh(follow_up)
    return follow_up


def send_follow_up(db: Session, follow_up: FollowUp, to: str) -> FollowUp:
    """Owner approved the follow-up: send it in the original thread and push the
    application's next follow-up due date forward."""
    from app.applications.review import SendDelayError, seconds_until_send_allowed
    from app.email_send.gmail_client import send_reply

    application = follow_up.application
    if not application.gmail_thread_id or not application.gmail_message_id:
        raise ValueError("Original application has no Gmail thread - nothing to follow up in.")

    wait = seconds_until_send_allowed(db)
    if wait > 0:
        raise SendDelayError(wait)

    result = send_reply(
        thread_id=application.gmail_thread_id,
        prior_gmail_message_id=application.gmail_message_id,
        to=to,
        subject=application.subject,
        body_text=follow_up.body,
    )

    follow_up.state = FollowUpState.SENT
    follow_up.sent_at = datetime.now(timezone.utc)
    follow_up.gmail_message_id = result["id"]
    application.follow_up_due_at = next_follow_up_due(follow_up.sent_at)
    db.commit()
    db.refresh(follow_up)
    return follow_up
