"""Application review UI (CLAUDE.md section 5 steps 5-6): approve -> send, or
reject with feedback -> regenerate, looped until approved. Also manual state
override (for replies that arrive outside Gmail - section 9: email is the
definitive channel, other channels are informational only) and a reply check."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.applications.drafting import regenerate_draft
from app.applications.review import approve_and_send
from app.db import get_db
from app.email_send.gmail_client import thread_has_reply
from app.llm.base import LLMProvider
from app.models import CV, Application, ApplicationState, Contact
from app.web.deps import get_llm
from app.web.render import render

router = APIRouter(prefix="/applications", tags=["applications"])
templates = Jinja2Templates(directory="app/web/templates")


def _application_or_404(db: Session, application_id: int) -> Application:
    application = db.get(Application, application_id)
    if application is None:
        raise ValueError(f"Application {application_id} not found")
    return application


def _resolve_recipient(db: Session, application: Application) -> str:
    contact = db.query(Contact).filter(Contact.application_id == application.id).first()
    return contact.email if contact else (application.company.contact_email or "")


@router.get("/{application_id}")
def application_detail(request: Request, application_id: int, db: Session = Depends(get_db)):
    application = _application_or_404(db, application_id)
    return render(
        templates,
        request,
        "application_detail.html",
        {
            "application": application,
            "states": list(ApplicationState),
            "recipient": _resolve_recipient(db, application),
            "cvs": db.query(CV).all(),
        },
    )


@router.post("/{application_id}/cv")
def set_cv(application_id: int, cv_id: str = Form(""), db: Session = Depends(get_db)):
    """CLAUDE.md section 9: a CV must always be attached - lets the owner fix or
    override the AI's pick (e.g. if it named a near-duplicate CV variant) without
    going through reject-with-feedback."""
    application = _application_or_404(db, application_id)
    application.cv_id = int(cv_id) if cv_id else None
    db.commit()
    return RedirectResponse(url=f"/applications/{application_id}", status_code=303)


@router.post("/{application_id}/approve")
def approve(application_id: int, db: Session = Depends(get_db)):
    application = _application_or_404(db, application_id)
    to = _resolve_recipient(db, application)
    approve_and_send(db, application, to)

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
    db: Session = Depends(get_db),
):
    """Direct owner edit of the AI draft - no LLM call. Lets the owner fix small
    things by hand instead of always going through reject-with-feedback."""
    application = _application_or_404(db, application_id)
    application.subject = subject
    application.cover_letter_body = cover_letter_body
    db.commit()
    return RedirectResponse(url=f"/applications/{application_id}", status_code=303)


@router.post("/{application_id}/reject")
def reject(
    application_id: int,
    feedback: str = Form(...),
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_llm),
):
    application = _application_or_404(db, application_id)
    regenerate_draft(db, llm, application, feedback.strip())
    return RedirectResponse(url=f"/applications/{application_id}", status_code=303)


@router.post("/{application_id}/state")
def set_state(application_id: int, state: str = Form(...), db: Session = Depends(get_db)):
    application = _application_or_404(db, application_id)
    application.state = ApplicationState(state)
    db.commit()
    return RedirectResponse(url=f"/applications/{application_id}", status_code=303)


@router.post("/{application_id}/check-reply")
def check_reply(application_id: int, db: Session = Depends(get_db)):
    application = _application_or_404(db, application_id)
    if application.gmail_thread_id and thread_has_reply(application.gmail_thread_id):
        application.state = ApplicationState.REPLIED
        db.commit()
    return RedirectResponse(url=f"/applications/{application_id}", status_code=303)
