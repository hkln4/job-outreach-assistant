"""Approve -> send (CLAUDE.md section 5 step 5, section 9 deliverability rules).
No auto-send - only ever called after explicit UI approval."""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.email_send.gmail_client import send_email, validate_email
from app.models import Application, ApplicationState, Contact, ContactState, FollowUp, FollowUpState

# Application states that mean "an email actually went out for this".
SENT_STATES = [
    ApplicationState.AWAITING_REPLY,
    ApplicationState.REPLIED,
    ApplicationState.INTERVIEW,
    ApplicationState.NO_RESPONSE,
    ApplicationState.REJECTED,
]


class DuplicateSendError(ValueError):
    """Section 9 dedup tripped: this company/address was already contacted.
    The UI turns this into a warning + explicit "send anyway" confirm, not a crash."""

    def __init__(self, prior: Application):
        self.prior = prior
        super().__init__(
            f"Already contacted before (application {prior.id}, "
            f"{prior.target_position or 'untitled'}) - confirm to send anyway."
        )


class SendDelayError(ValueError):
    """Section 9 deliverability: two sends were attempted too close together."""

    def __init__(self, wait_seconds: int):
        self.wait_seconds = wait_seconds
        super().__init__(f"Last send was too recent - wait {wait_seconds}s before sending again.")


def find_prior_send(
    db: Session, company_id: int, recipient_email: str, exclude_application_id: int | None = None
) -> Application | None:
    """Most recent application that already went out to this company OR this
    exact address (a contact at another company could share an address; a
    company's contact_email may have changed since the last send - matching on
    both catches either case). Returns the prior application for the warning UI."""
    query = db.query(Application).filter(Application.state.in_(SENT_STATES))
    if exclude_application_id is not None:
        query = query.filter(Application.id != exclude_application_id)

    company_prior = (
        query.filter(Application.company_id == company_id).order_by(Application.sent_at.desc()).first()
    )
    if company_prior is not None:
        return company_prior

    if recipient_email:
        contact = (
            db.query(Contact)
            .filter(Contact.email == recipient_email, Contact.state == ContactState.SENT)
            .first()
        )
        if contact is not None and contact.application_id is not None:
            prior = db.get(Application, contact.application_id)
            if prior is not None and (exclude_application_id is None or prior.id != exclude_application_id):
                return prior
    return None


def seconds_until_send_allowed(db: Session, now: datetime | None = None) -> int:
    """Deliverability delay (section 9): how long until the next send is allowed,
    based on the most recent application or follow-up send. 0 = allowed now."""
    settings = get_settings()
    if settings.send_delay_seconds <= 0:
        return 0
    now = now or datetime.now(timezone.utc)

    last_app = db.query(Application).filter(Application.sent_at.isnot(None)).order_by(Application.sent_at.desc()).first()
    last_follow_up = db.query(FollowUp).filter(FollowUp.sent_at.isnot(None)).order_by(FollowUp.sent_at.desc()).first()

    last_sent_at = None
    for candidate in (last_app.sent_at if last_app else None, last_follow_up.sent_at if last_follow_up else None):
        if candidate is not None and (last_sent_at is None or candidate > last_sent_at):
            last_sent_at = candidate
    if last_sent_at is None:
        return 0

    if last_sent_at.tzinfo is None:  # SQLite drops tzinfo on read
        last_sent_at = last_sent_at.replace(tzinfo=timezone.utc)
    elapsed = (now - last_sent_at).total_seconds()
    return max(0, int(settings.send_delay_seconds - elapsed))


def approve_and_send(db: Session, application: Application, to: str, allow_duplicate: bool = False) -> Application:
    if not validate_email(to):
        raise ValueError(f"Invalid recipient address: {to!r}")
    if not allow_duplicate:
        prior = find_prior_send(db, application.company_id, to, exclude_application_id=application.id)
        if prior is not None:
            raise DuplicateSendError(prior)
    wait = seconds_until_send_allowed(db)
    if wait > 0:
        raise SendDelayError(wait)

    attachment_path = application.cv.file_path if application.cv else None
    result = send_email(
        to=to,
        subject=application.subject,
        body_text=application.cover_letter_body,
        attachment_path=attachment_path,
    )

    from app.applications.follow_ups import next_follow_up_due  # local import to avoid a cycle

    application.state = ApplicationState.AWAITING_REPLY
    application.gmail_message_id = result["id"]
    application.gmail_thread_id = result["threadId"]
    application.sent_at = datetime.now(timezone.utc)
    application.follow_up_due_at = next_follow_up_due(application.sent_at)
    db.commit()
    db.refresh(application)
    return application
