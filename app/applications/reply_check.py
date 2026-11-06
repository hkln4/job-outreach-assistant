"""Reply checking (CLAUDE.md section 5 step 6): owner-triggered, one application
or all AWAITING_REPLY ones at once. Also the NO_RESPONSE-threshold query that
feeds the dashboard's "no reply yet" queue - the state change itself stays a
manual owner action, this module never flips anything to NO_RESPONSE."""
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.channels.domains import registrable_domain
from app.config import get_settings
from app.email_send.gmail_client import classify_thread, possible_replies, validate_email
from app.models import Application, ApplicationState, Contact, ContactState

# Auto-checks (page loads) skip applications checked more recently than this,
# so browsing around doesn't hammer the Gmail API. Explicit button/bulk checks
# ignore it - an owner click always means "check now".
AUTO_CHECK_MIN_INTERVAL = timedelta(minutes=30)


# Replies from these come from anyone - searching the domain would be noise.
FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "live.com", "yahoo.com",
    "yandex.com", "yandex.com.tr", "icloud.com", "proton.me", "protonmail.com", "mynet.com",
}


def _company_domain(application: Application) -> str:
    """The domain a reply would come from: the address we wrote to, else the
    company's website."""
    address = application.sent_to or ""
    domain = address.split("@", 1)[1].lower() if "@" in address else ""
    if not domain and application.company and application.company.website_url:
        domain = registrable_domain(application.company.website_url)
    return "" if domain in FREE_MAIL_DOMAINS else domain


def _add_note(application: Application, note: dict) -> bool:
    """Append a note unless one for the same message is already there."""
    notes = list(application.notes or [])
    if note.get("message_id") and any(n.get("message_id") == note["message_id"] for n in notes):
        return False
    notes.append({**note, "at": datetime.now(timezone.utc).isoformat()})
    application.notes = notes
    return True


def mark_bounced(db: Session, application: Application, detail: str = "") -> None:
    """BOUNCED + the address recorded as invalid, so it is never offered
    again (research emails are flagged, the contact email is cleared)."""
    application.state = ApplicationState.BOUNCED
    address = (application.sent_to or "").strip().lower()
    failed = [a.strip().lower() for a in detail.replace(";", ",").split(",") if "@" in a]
    invalid = set(failed or ([address] if address else []))
    company = application.company
    if company is not None and invalid:
        company.invalid_emails = sorted(set(company.invalid_emails or []) | invalid)
        research = dict(company.research or {})
        if research.get("emails"):
            research["emails"] = [{**e, "invalid": e.get("email", "").lower() in invalid or e.get("invalid", False)} for e in research["emails"]]
            company.research = research
        if (company.contact_email or "").strip().lower() in invalid:
            company.contact_email = None
    for contact in db.query(Contact).filter(Contact.application_id == application.id).all():
        if contact.email.strip().lower() in invalid:
            contact.state = ContactState.NO_RESPONSE


def alternative_emails(application: Application) -> list[dict]:
    """Other known addresses at the company after a bounce: research emails
    (HR first) that never bounced, then contacts' addresses."""
    company = application.company
    if company is None:
        return []
    invalid = {e.lower() for e in company.invalid_emails or []} | {(application.sent_to or "").lower()}
    seen: set[str] = set()
    options = []
    emails = sorted((company.research or {}).get("emails") or [], key=lambda e: e.get("kind") != "hr")
    candidates = [(e.get("email", ""), e.get("context", "")) for e in emails]
    candidates += [(c.email, f"{c.name} - {c.role}".strip(" -")) for c in company.contacts]
    for address, context in candidates:
        key = (address or "").strip().lower()
        if key and key not in invalid and key not in seen and validate_email(key):
            seen.add(key)
            options.append({"email": address.strip(), "context": context})
    return options


def resend_after_bounce(db: Session, application: Application, to: str) -> Application:
    """A fresh PENDING_APPROVAL copy of a bounced letter. The company's contact
    address becomes `to`, so Approve sends there; dedup ignores the bounced
    application (it never reached anyone)."""
    company = application.company
    company.contact_email = to.strip()
    copy = Application(
        company_id=application.company_id,
        cv_id=application.cv_id,
        job_posting_id=application.job_posting_id,
        target_position=application.target_position,
        application_reason=application.application_reason,
        subject=application.subject,
        cover_letter_body=application.cover_letter_body,
        sources=application.sources,
        evidence=application.evidence,
        revision_history=[{**((application.revision_history or [{}])[0]), "resend_of": application.id}],
        state=ApplicationState.PENDING_APPROVAL,
    )
    db.add(copy)
    db.commit()
    db.refresh(copy)
    return copy


def check_application_reply(db: Session, application: Application) -> str:
    """One-shot check of this application's own thread. Returns the kind found:
    "reply" (state -> REPLIED), "bounce" (-> BOUNCED, address marked invalid),
    "auto_reply" (a note; still AWAITING_REPLY) or "none". With nothing in the
    thread, the company's domain is searched for a reply sent outside it -
    recorded as a "possible reply" note for the owner to confirm. Always
    stamps last_checked_at."""
    if not application.gmail_thread_id:
        return "none"

    result = classify_thread(application.gmail_thread_id)
    kind = result["kind"]
    application.last_checked_at = datetime.now(timezone.utc)
    awaiting = application.state == ApplicationState.AWAITING_REPLY
    if kind == "reply" and awaiting:
        application.state = ApplicationState.REPLIED
    elif kind == "bounce" and awaiting:
        _add_note(application, {"kind": "bounce", "detail": result["detail"], "message_id": result["message_id"]})
        mark_bounced(db, application, result["detail"])
    elif kind == "auto_reply":
        _add_note(application, {"kind": "auto_reply", "detail": result["detail"], "message_id": result["message_id"]})

    if kind in ("none", "auto_reply") and awaiting and application.sent_at is not None:
        sent_at = application.sent_at if application.sent_at.tzinfo else application.sent_at.replace(tzinfo=timezone.utc)
        try:
            found = possible_replies(_company_domain(application), sent_at, application.gmail_thread_id)
        except Exception:  # noqa: BLE001 - the thread result above still counts
            found = []
        for message in found:
            _add_note(
                application,
                {
                    "kind": "possible_reply",
                    "detail": f"{message['from']}: {message['subject']}",
                    "message_id": message["id"],
                    "date": message["date"],
                },
            )
    db.commit()
    return kind


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
    """Bulk check for the dashboard button. Returns {"checked": n, "replies": n,
    "bounces": n}. A Gmail failure on one thread doesn't abort the rest."""
    applications = (
        db.query(Application)
        .filter(Application.state == ApplicationState.AWAITING_REPLY, Application.gmail_thread_id.isnot(None))
        .all()
    )
    checked = 0
    replies = 0
    bounces = 0
    for application in applications:
        try:
            kind = check_application_reply(db, application)
            replies += kind == "reply"
            bounces += kind == "bounce"
            checked += 1
        except Exception:  # noqa: BLE001 - keep going; the per-thread result just stays unknown
            db.rollback()
    return {"checked": checked, "replies": replies, "bounces": bounces}


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
