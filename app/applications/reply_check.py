"""Reply checking (CLAUDE.md section 5 step 6): owner-triggered, one application
or all AWAITING_REPLY ones at once. Also the NO_RESPONSE-threshold query that
feeds the dashboard's "no reply yet" queue - the state change itself stays a
manual owner action, this module never flips anything to NO_RESPONSE."""
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.email_send.gmail_client import thread_has_reply
from app.models import Application, ApplicationState

# Auto-checks (page loads) skip applications checked more recently than this,
# so browsing around doesn't hammer the Gmail API. Explicit button/bulk checks
# ignore it - an owner click always means "check now".
AUTO_CHECK_MIN_INTERVAL = timedelta(minutes=30)


def check_application_reply(db: Session, application: Application) -> bool:
    """One-shot check of this application's own thread. Returns True if a reply
    was found (state moves to REPLIED). Always stamps last_checked_at."""
    if not application.gmail_thread_id:
        return False

    replied = thread_has_reply(application.gmail_thread_id)
    application.last_checked_at = datetime.now(timezone.utc)
    if replied and application.state == ApplicationState.AWAITING_REPLY:
        application.state = ApplicationState.REPLIED
    db.commit()
    return replied


def auto_check_if_due(db: Session, application: Application) -> None:
    """Page-load hook: check quietly if this application is AWAITING_REPLY and
    hasn't been checked recently. Gmail errors (not authed yet, offline) are
    swallowed - a detail page must never fail because a background nicety did."""
    if application.state != ApplicationState.AWAITING_REPLY or not application.gmail_thread_id:
        return
    last = application.last_checked_at
    if last is not None:
        if last.tzinfo is None:  # SQLite drops tzinfo on read
            last = last.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - last < AUTO_CHECK_MIN_INTERVAL:
            return
    try:
        check_application_reply(db, application)
    except Exception:  # noqa: BLE001 - the manual button surfaces errors; this path stays quiet
        db.rollback()


def check_all_awaiting(db: Session) -> dict:
    """Bulk check for the dashboard button. Returns {"checked": n, "replies": n}.
    A Gmail failure on one thread doesn't abort the rest."""
    applications = (
        db.query(Application)
        .filter(Application.state == ApplicationState.AWAITING_REPLY, Application.gmail_thread_id.isnot(None))
        .all()
    )
    checked = 0
    replies = 0
    for application in applications:
        try:
            if check_application_reply(db, application):
                replies += 1
            checked += 1
        except Exception:  # noqa: BLE001 - keep going; the per-thread result just stays unknown
            db.rollback()
    return {"checked": checked, "replies": replies}


def overdue_applications(db: Session, now: datetime | None = None) -> list[Application]:
    """AWAITING_REPLY applications whose follow-up is due (no reply for
    NO_RESPONSE_DAYS since the last touch). Owner decides what happens next:
    follow up, escalate to a contact, or mark NO_RESPONSE."""
    now = now or datetime.now(timezone.utc)
    settings = get_settings()
    fallback_cutoff = now - timedelta(days=settings.no_response_days)

    candidates = (
        db.query(Application)
        .filter(Application.state == ApplicationState.AWAITING_REPLY)
        .order_by(Application.sent_at.asc())
        .all()
    )
    overdue = []
    for application in candidates:
        due = application.follow_up_due_at
        if due is None:
            # sent before follow_up_due_at existed - fall back to sent_at + threshold
            due = application.sent_at
            if due is None:
                continue
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            if due <= fallback_cutoff:
                overdue.append(application)
            continue
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        if due <= now:
            overdue.append(application)
    return overdue
