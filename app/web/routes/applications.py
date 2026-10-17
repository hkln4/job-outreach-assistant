"""Application review UI (CLAUDE.md section 5 steps 5-6): approve -> send, or
reject with feedback -> regenerate, looped until approved. Also manual state
override (for replies that arrive outside Gmail - section 9: email is the
definitive channel, other channels are informational only), reply checks
(manual + quiet page-load auto-check), the cross-company applications index,
and the follow-up (same-thread reminder) flow."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.applications.draft_checks import draft_warnings
from app.applications.drafting import regenerate_draft
from app.applications.follow_ups import draft_follow_up, send_follow_up
from app.applications.reply_check import auto_check_if_due, check_application_reply
from app.applications.review import (
    SENT_STATES,
    AlreadySentError,
    DuplicateSendError,
    SendDelayError,
    StateTransitionError,
    allowed_state_targets,
    approve_and_send,
    find_prior_send,
    is_sent,
    set_application_state,
)
from app.db import get_db
from app.llm.base import LLMProvider
from app.models import (
    CV,
    Application,
    ApplicationState,
    Company,
    Contact,
    ContactState,
    FollowUp,
    FollowUpState,
)
from app.web.deps import get_draft_llm
from app.web.errors import NotFoundError
from app.web.render import render

router = APIRouter(prefix="/applications", tags=["applications"])
templates = Jinja2Templates(directory="app/web/templates")


def _application_or_404(db: Session, application_id: int) -> Application:
    application = db.get(Application, application_id)
    if application is None:
        raise NotFoundError(f"Application {application_id} not found")
    return application


def _blocked_redirect(application_id: int) -> RedirectResponse:
    """Back to the detail page with a notice: the action would have changed or
    re-sent a letter that already went out."""
    return RedirectResponse(url=f"/applications/{application_id}?blocked=1", status_code=303)


def _resolve_recipient(db: Session, application: Application) -> str:
    contact = db.query(Contact).filter(Contact.application_id == application.id).first()
    return contact.email if contact else (application.company.contact_email or "")


@router.get("")
def applications_index(
    request: Request,
    state: str = "",
    company_id: int | None = None,
    db: Session = Depends(get_db),
):
    """Retained history of ALL applications across companies (CLAUDE.md section
    10), filterable by state and company."""
    query = db.query(Application)
    if state:
        query = query.filter(Application.state == ApplicationState(state))
    if company_id:
        query = query.filter(Application.company_id == company_id)
    applications = query.order_by(Application.last_action_at.desc()).all()
    return render(
        templates,
        request,
        "applications.html",
        {
            "applications": applications,
            "states": list(ApplicationState),
            "companies": db.query(Company).order_by(Company.name).all(),
            "filter_state": state,
            "filter_company_id": company_id,
        },
    )


@router.get("/{application_id}")
def application_detail(
    request: Request,
    application_id: int,
    duplicate: int = 0,
    wait: int = 0,
    blocked: int = 0,
    db: Session = Depends(get_db),
):
    application = _application_or_404(db, application_id)
    auto_check_if_due(db, application)  # quiet reply auto-check; button stays the explicit path

    recipient = _resolve_recipient(db, application)
    prior_send = None
    if application.state == ApplicationState.PENDING_APPROVAL:
        prior_send = find_prior_send(
            db, application.company_id, recipient, exclude_application_id=application.id
        )

    follow_ups = (
        db.query(FollowUp)
        .filter(FollowUp.application_id == application.id)
        .order_by(FollowUp.created_at.asc())
        .all()
    )
    return render(
        templates,
        request,
        "application_detail.html",
        {
            "application": application,
            "allowed_states": allowed_state_targets(application),
            "recipient": recipient,
            "cvs": db.query(CV).all(),
            "prior_send": prior_send,
            "duplicate_blocked": bool(duplicate),
            "wait_seconds": wait,
            "blocked": bool(blocked),
            "draft_warnings": draft_warnings(application),
            "follow_ups": follow_ups,
            "pending_follow_up": next(
                (f for f in follow_ups if f.state == FollowUpState.PENDING_APPROVAL), None
            ),
        },
    )


@router.post("/{application_id}/cv")
def set_cv(application_id: int, cv_id: str = Form(""), db: Session = Depends(get_db)):
    """CLAUDE.md section 9: a CV must always be attached - lets the owner fix or
    override the AI's pick (e.g. if it named a near-duplicate CV variant) without
    going through reject-with-feedback."""
    application = _application_or_404(db, application_id)
    if is_sent(application):  # the attachment that went out is part of the record
        return _blocked_redirect(application_id)
    application.cv_id = int(cv_id) if cv_id else None
    db.commit()
    return RedirectResponse(url=f"/applications/{application_id}", status_code=303)


@router.post("/{application_id}/approve")
def approve(application_id: int, force: str = Form(""), db: Session = Depends(get_db)):
    application = _application_or_404(db, application_id)
    to = _resolve_recipient(db, application)
    try:
        approve_and_send(db, application, to, allow_duplicate=bool(force))
    except AlreadySentError:
        # A double-click, a second tab, or a stale page - never a second email.
        return _blocked_redirect(application_id)
    except DuplicateSendError:
        # Not a crash: the detail page shows the evidence and a "send anyway" confirm.
        return RedirectResponse(url=f"/applications/{application_id}?duplicate=1", status_code=303)
    except SendDelayError as exc:
        return RedirectResponse(
            url=f"/applications/{application_id}?wait={exc.wait_seconds}", status_code=303
        )

    contact = db.query(Contact).filter(Contact.application_id == application.id).first()
    if contact is not None:
        from app.models import ContactState

        contact.state = ContactState.SENT
        db.commit()

    return RedirectResponse(url=f"/applications/{application_id}", status_code=303)


@router.post("/{application_id}/edit")
def edit_application(
    application_id: int,
    subject: str = Form(...),
    cover_letter_body: str = Form(...),
    target_position: str = Form(""),
    db: Session = Depends(get_db),
):
    """Direct owner edit of the AI draft - no LLM call. Lets the owner fix small
    things (including the target position) by hand instead of always going
    through reject-with-feedback."""
    application = _application_or_404(db, application_id)
    if is_sent(application):  # what was sent is history, not a draft
        return _blocked_redirect(application_id)
    application.subject = subject
    application.cover_letter_body = cover_letter_body
    application.target_position = target_position.strip()
    db.commit()
    return RedirectResponse(url=f"/applications/{application_id}", status_code=303)


@router.post("/{application_id}/reject")
def reject(
    application_id: int,
    feedback: str = Form(...),
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_draft_llm),
):
    application = _application_or_404(db, application_id)
    try:
        regenerate_draft(db, llm, application, feedback.strip())
    except AlreadySentError:
        return _blocked_redirect(application_id)
    return RedirectResponse(url=f"/applications/{application_id}", status_code=303)


@router.post("/{application_id}/evidence/{item_id}/toggle")
def toggle_evidence(application_id: int, item_id: int, db: Session = Depends(get_db)):
    """Owner marks a retrieved profile record as "don't use in this draft" (or
    takes that back). Applies at the next regenerate, which then picks again
    without it."""
    application = _application_or_404(db, application_id)
    if is_sent(application):
        return _blocked_redirect(application_id)
    excluded = list(application.excluded_evidence_ids or [])
    if item_id in excluded:
        excluded.remove(item_id)
    else:
        excluded.append(item_id)
    application.excluded_evidence_ids = excluded  # reassigned: JSON columns don't track in-place edits
    db.commit()
    return RedirectResponse(url=f"/applications/{application_id}#evidence", status_code=303)


@router.post("/{application_id}/state")
def set_state(application_id: int, state: str = Form(...), db: Session = Depends(get_db)):
    application = _application_or_404(db, application_id)
    try:
        set_application_state(db, application, ApplicationState(state))
    except StateTransitionError:
        return _blocked_redirect(application_id)
    return RedirectResponse(url=f"/applications/{application_id}", status_code=303)


@router.post("/{application_id}/delete")
def delete_application(application_id: int, db: Session = Depends(get_db)):
    """Discard a draft that shouldn't have been started - the way back from a
    mis-click on "start application". Only before it goes out: once sent, the
    application is history and CLAUDE.md section 5 step 6 keeps it.

    Frees the company to reappear in the dashboard's "ready to draft" queue.
    """
    application = _application_or_404(db, application_id)
    company_id = application.company_id
    if application.state in SENT_STATES or application.sent_at is not None:
        return RedirectResponse(url=f"/applications/{application_id}", status_code=303)

    # Neither of these is cleaned up by SQLAlchemy on delete: FollowUp has a
    # non-nullable FK, and Contact.application_id has no ORM relationship at all
    # (an escalation draft would leave the contact stuck in DRAFT forever).
    db.query(FollowUp).filter(FollowUp.application_id == application_id).delete()
    for contact in db.query(Contact).filter(Contact.application_id == application_id).all():
        contact.application_id = None
        contact.state = ContactState.NOT_CONTACTED
    db.delete(application)
    db.commit()
    return RedirectResponse(url=f"/companies/{company_id}", status_code=303)


@router.post("/{application_id}/check-reply")
def check_reply(application_id: int, db: Session = Depends(get_db)):
    application = _application_or_404(db, application_id)
    check_application_reply(db, application)
    return RedirectResponse(url=f"/applications/{application_id}", status_code=303)


# --- Follow-ups (same-thread reminders; owner-approved like everything else) ---


@router.post("/{application_id}/follow-ups/draft")
def create_follow_up(
    application_id: int, db: Session = Depends(get_db), llm: LLMProvider = Depends(get_draft_llm)
):
    application = _application_or_404(db, application_id)
    draft_follow_up(db, llm, application)
    return RedirectResponse(url=f"/applications/{application_id}", status_code=303)


def _follow_up_or_404(db: Session, application_id: int, follow_up_id: int) -> FollowUp:
    follow_up = db.get(FollowUp, follow_up_id)
    if follow_up is None or follow_up.application_id != application_id:
        raise ValueError(f"Follow-up {follow_up_id} not found for application {application_id}")
    return follow_up


@router.post("/{application_id}/follow-ups/{follow_up_id}/edit")
def edit_follow_up(
    application_id: int, follow_up_id: int, body: str = Form(...), db: Session = Depends(get_db)
):
    follow_up = _follow_up_or_404(db, application_id, follow_up_id)
    follow_up.body = body
    db.commit()
    return RedirectResponse(url=f"/applications/{application_id}", status_code=303)


@router.post("/{application_id}/follow-ups/{follow_up_id}/send")
def approve_follow_up(application_id: int, follow_up_id: int, db: Session = Depends(get_db)):
    application = _application_or_404(db, application_id)
    follow_up = _follow_up_or_404(db, application_id, follow_up_id)
    to = _resolve_recipient(db, application)
    try:
        send_follow_up(db, follow_up, to)
    except SendDelayError as exc:
        return RedirectResponse(
            url=f"/applications/{application_id}?wait={exc.wait_seconds}", status_code=303
        )
    return RedirectResponse(url=f"/applications/{application_id}", status_code=303)


@router.post("/{application_id}/follow-ups/{follow_up_id}/delete")
def delete_follow_up(application_id: int, follow_up_id: int, db: Session = Depends(get_db)):
    follow_up = _follow_up_or_404(db, application_id, follow_up_id)
    if follow_up.state == FollowUpState.PENDING_APPROVAL:  # sent history is retained
        db.delete(follow_up)
        db.commit()
    return RedirectResponse(url=f"/applications/{application_id}", status_code=303)
