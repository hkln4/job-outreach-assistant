"""Approve -> send (CLAUDE.md section 5 step 5, section 9 deliverability rules).
No auto-send - only ever called after explicit UI approval."""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.email_send.gmail_client import send_email, validate_email
from app.models import Application, ApplicationState, Company, Contact, ContactState


def has_prior_send(db: Session, recipient_email: str) -> bool:
    """Section 9 dedup: never send to the same company/contact address twice."""
    sent_states = [
        ApplicationState.SENT,
        ApplicationState.AWAITING_REPLY,
        ApplicationState.REPLIED,
        ApplicationState.INTERVIEW,
        ApplicationState.NO_RESPONSE,
        ApplicationState.REJECTED,
    ]
    company_sent = (
        db.query(Application)
        .join(Company, Application.company_id == Company.id)
        .filter(Company.contact_email == recipient_email, Application.state.in_(sent_states))
        .first()
    )
    if company_sent is not None:
        return True

    contact_sent = (
        db.query(Contact).filter(Contact.email == recipient_email, Contact.state == ContactState.SENT).first()
    )
    return contact_sent is not None


def approve_and_send(db: Session, application: Application, to: str) -> Application:
    if not validate_email(to):
        raise ValueError(f"Invalid recipient address: {to!r}")
    if has_prior_send(db, to):
        raise ValueError(f"Already sent to {to} before - dedup rule blocks resending.")

    attachment_path = application.cv.file_path if application.cv else None
    result = send_email(
        to=to,
        subject=application.subject,
        body_text=application.cover_letter_body,
        attachment_path=attachment_path,
    )

    application.state = ApplicationState.AWAITING_REPLY
    application.gmail_message_id = result["id"]
    application.gmail_thread_id = result["threadId"]
    application.sent_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(application)
    return application
