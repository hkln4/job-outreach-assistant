"""Approve -> send (CLAUDE.md section 5 step 5, section 9 deliverability rules).
No auto-send - only ever called after explicit UI approval."""
import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.email_send.gmail_client import find_sent_message, new_message_id, send_email, validate_email
from app.models import Application, ApplicationState, Contact, ContactState, FollowUp, FollowUpState
from app.web.files import safe_component

# Application states that mean "an email actually went out for this".
SENT_STATES = [
    ApplicationState.AWAITING_REPLY,
    ApplicationState.REPLIED,
    ApplicationState.INTERVIEW,
    ApplicationState.NO_RESPONSE,
    ApplicationState.REJECTED,
    ApplicationState.BOUNCED,
]
# A bounced email never reached the company, so it doesn't count as "already
# contacted" for the dedup rule - resending to another address is the point.
CONTACTED_STATES = [state for state in SENT_STATES if state != ApplicationState.BOUNCED]


class AlreadySentError(ValueError):
    """This application already went out. Sending it again would email the same
    company the same letter twice - never allowed, not even with the duplicate
    override (that one is about a *different* application to the same place)."""

    def __init__(self, application: Application):
        self.application = application
        super().__init__(f"Application {application.id} was already sent - it is never sent twice.")


class StateTransitionError(ValueError):
    """A manual state change the lifecycle doesn't allow (section 7)."""


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


def is_sent(application: Application) -> bool:
    """Any trace of a real send counts - the state alone can be edited by hand,
    the Gmail ids and sent_at can't."""
    return (
        application.sent_at is not None
        or bool(application.gmail_message_id)
        or application.state in SENT_STATES
    )


def ensure_not_sent(application: Application) -> None:
    """Guard for anything that would change or re-send the letter itself
    (approve, edit, regenerate, CV swap): once sent, the record is history."""
    if is_sent(application):
        raise AlreadySentError(application)


def allowed_state_targets(application: Application) -> list[ApplicationState]:
    """States the owner may set by hand. A sent application stays inside the
    post-send lifecycle (replies that arrived outside Gmail, an interview, ...);
    moving it back to PENDING_APPROVAL would re-arm Approve and send the same
    email twice. An unsent one can't be marked as sent without actually sending."""
    if is_sent(application):
        return list(SENT_STATES)
    return [ApplicationState.DRAFT, ApplicationState.PENDING_APPROVAL]


def set_application_state(db: Session, application: Application, new_state: ApplicationState) -> Application:
    if new_state not in allowed_state_targets(application):
        raise StateTransitionError(
            f"Application {application.id} can't move from {application.state.value} to {new_state.value}."
        )
    application.state = new_state
    if new_state in (ApplicationState.REPLIED, ApplicationState.INTERVIEW) and application.replied_at is None:
        application.replied_at = datetime.now(timezone.utc)
    db.commit()
    return application


def find_prior_send(
    db: Session, company_id: int, recipient_email: str, exclude_application_id: int | None = None
) -> Application | None:
    """Most recent application that already went out to this company OR this
    exact address (a contact at another company could share an address; a
    company's contact_email may have changed since the last send - matching on
    both catches either case). Returns the prior application for the warning UI."""
    query = db.query(Application).filter(Application.state.in_(CONTACTED_STATES))
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


def _find_after_failed_send(message_id: str, to: str, subject: str, after: datetime) -> dict | None:
    try:
        return find_sent_message(message_id, to, subject, after)
    except Exception:  # noqa: BLE001 - the original send error is the one to show
        return None


def attachment_source(db: Session, application: Application) -> tuple[Path, str, int | None] | None:
    """(file, display name, cv_id) of what this application attaches: an
    approved generated CV (phase 5) wins over the chosen CV variant."""
    if application.generated_cv_id:
        from app.models import GeneratedCV

        generated = db.get(GeneratedCV, application.generated_cv_id)
        if generated is not None and generated.status == "approved" and generated.pdf_path:
            from app.applications.signature import owner_identity

            name = owner_identity(db).get("full_name") or "CV"
            return Path(generated.pdf_path), f"{safe_component(name)}_CV.pdf", None
    cv = application.cv
    if cv is not None and cv.file_path:
        source = Path(cv.file_path)
        return source, f"{safe_component(cv.name)}{source.suffix}", cv.id
    return None


def snapshot_cv(db: Session, application: Application) -> dict:
    """Copy the file about to be attached to SENT_CVS_DIR and describe it:
    {path, sha256, name}. The copy is what gets attached, so the record and
    the email can't differ."""
    found = attachment_source(db, application)
    if found is None:
        return {}
    source, name, cv_id = found
    if not source.is_file():
        raise ValueError(f"The CV file for {name!r} is missing ({source}) - re-upload it before sending.")
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    sent_dir = Path(get_settings().sent_cvs_dir)
    sent_dir.mkdir(parents=True, exist_ok=True)
    target = sent_dir / f"application_{application.id}_{digest[:12]}{source.suffix}"
    if not target.exists():
        shutil.copyfile(source, target)
    return {
        "path": str(target),
        "sha256": digest,
        "name": name,
        "cv_id": cv_id,
        "generated_cv_id": application.generated_cv_id if cv_id is None else None,
        "original_path": str(source),
    }


def approve_and_send(db: Session, application: Application, to: str, allow_duplicate: bool = False) -> Application:
    ensure_not_sent(application)
    if application.state != ApplicationState.PENDING_APPROVAL:
        raise ValueError(f"Application {application.id} is not awaiting approval ({application.state.value}).")
    if not validate_email(to):
        raise ValueError(f"Invalid recipient address: {to!r}")
    if not allow_duplicate:
        prior = find_prior_send(db, application.company_id, to, exclude_application_id=application.id)
        if prior is not None:
            raise DuplicateSendError(prior)
    wait = seconds_until_send_allowed(db)
    if wait > 0:
        raise SendDelayError(wait)

    # Copy the CV first: a missing file must stop the send before the claim.
    snapshot = snapshot_cv(db, application)

    # Claim the send before talking to Gmail: a conditional UPDATE only one
    # request can win (SQLite serializes writes), so a double-click or two tabs
    # can't both get past the checks above and send twice. Undone if Gmail fails.
    claimed = (
        db.query(Application)
        .filter(
            Application.id == application.id,
            Application.state == ApplicationState.PENDING_APPROVAL,
            Application.sent_at.is_(None),
        )
        .update({Application.state: ApplicationState.AWAITING_REPLY}, synchronize_session=False)
    )
    db.commit()
    db.refresh(application)
    if not claimed:
        raise AlreadySentError(application)

    message_id = new_message_id()
    application.rfc_message_id = message_id
    db.commit()
    attempted_at = datetime.now(timezone.utc)
    try:
        result = send_email(
            to=to,
            subject=application.subject,
            body_text=application.cover_letter_body,
            attachment_path=snapshot.get("path") or None,
            attachment_name=snapshot.get("name") or None,
            message_id=message_id,
        )
    except Exception:
        # A timeout can come after Gmail accepted the message: look in Sent
        # before un-claiming, or Approve would be re-armed and send it twice.
        result = _find_after_failed_send(message_id, to, application.subject, attempted_at)
        if result is None:
            application.state = ApplicationState.PENDING_APPROVAL
            application.rfc_message_id = None
            db.commit()
            raise
        application.notes = list(application.notes or []) + [
            {"kind": "send_recovered", "detail": "Gmail raised, but the message was found in Sent.", "at": attempted_at.isoformat()}
        ]

    from app.applications.follow_ups import next_follow_up_due  # local import to avoid a cycle

    application.state = ApplicationState.AWAITING_REPLY
    application.gmail_message_id = result["id"]
    application.gmail_thread_id = result["threadId"]
    application.sent_to = to
    application.cv_snapshot = snapshot
    application.sent_at = datetime.now(timezone.utc)
    application.follow_up_due_at = next_follow_up_due(application.sent_at)
    if application.job_posting is not None:
        from app.models import PostingStatus

        application.job_posting.status = PostingStatus.APPLIED
    db.commit()
    db.refresh(application)
    return application


FORM_CHANNELS = ("site_form", "ats_form", "contact_form")


def mark_submitted(db: Session, application: Application, channel: str, form_url: str = "", allow_duplicate: bool = False) -> Application:
    """The owner filled and submitted a form himself (the app never does):
    record the channel and move to AWAITING_REPLY. Same dedup rule as email."""
    ensure_not_sent(application)
    if channel not in FORM_CHANNELS:
        raise ValueError(f"Unknown form channel {channel!r}.")
    if not allow_duplicate:
        prior = find_prior_send(db, application.company_id, "", exclude_application_id=application.id)
        if prior is not None:
            raise DuplicateSendError(prior)
    from app.applications.follow_ups import next_follow_up_due

    application.channel = channel
    application.sent_to = form_url
    application.state = ApplicationState.AWAITING_REPLY
    application.sent_at = datetime.now(timezone.utc)
    application.follow_up_due_at = next_follow_up_due(application.sent_at)
    if application.job_posting is not None:
        from app.models import PostingStatus

        application.job_posting.status = PostingStatus.APPLIED
    db.commit()
    return application
