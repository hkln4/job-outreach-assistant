"""Company detail page (CLAUDE.md section 5 step 2): read-only research findings
(auto-fetched on creation - see companies.py) plus evidence-first preference
warnings with a hard-exclusion confirm gate. Drafting/approving applications is
dashboard-only now; this page keeps the /draft route only because the dashboard's
"ready to draft" action posts to it. Also hosts the Contacts (employee fallback)
CRUD for that company."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from datetime import datetime, timezone

from app.applications.drafting import create_draft_application
from app.applications.escalation import create_escalation_draft
from app.applications.review import SENT_STATES
from app.companies.workflow import has_unconfirmed_hard_exclusion, research_company_safe
from app.db import get_db
from app.llm.base import LLMProvider
from app.models import Application, Company, Contact, FollowUp, FollowUpState
from app.web.deps import get_llm
from app.web.render import render

router = APIRouter(prefix="/companies/{company_id}", tags=["company-detail"])
templates = Jinja2Templates(directory="app/web/templates")


def _company_or_404(db: Session, company_id: int) -> Company:
    company = db.get(Company, company_id)
    if company is None:
        raise ValueError(f"Company {company_id} not found")
    return company


def _build_timeline(applications: list[Application], follow_ups: list[FollowUp]) -> list[dict]:
    """All outreach touchpoints for this company, newest first - the 'have I
    talked to these people before, and when?' view."""
    events = []
    for application in applications:
        if application.sent_at is not None:
            events.append(
                {"at": application.sent_at, "kind": "application_sent", "application": application}
            )
        else:
            events.append(
                {"at": application.last_action_at, "kind": "draft", "application": application}
            )
    for follow_up in follow_ups:
        if follow_up.state == FollowUpState.SENT and follow_up.sent_at is not None:
            events.append(
                {"at": follow_up.sent_at, "kind": "follow_up_sent", "application": follow_up.application}
            )
    return sorted(events, key=lambda e: e["at"] or datetime.min, reverse=True)


def _days_since(moment: datetime | None) -> int | None:
    if moment is None:
        return None
    if moment.tzinfo is None:  # SQLite drops tzinfo on read
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).days


@router.get("")
def company_detail(request: Request, company_id: int, db: Session = Depends(get_db)):
    company = _company_or_404(db, company_id)
    applications = (
        db.query(Application)
        .filter(Application.company_id == company_id)
        .order_by(Application.id.desc())
        .all()
    )
    contacts = db.query(Contact).filter(Contact.company_id == company_id).order_by(Contact.id.desc()).all()
    follow_ups = (
        db.query(FollowUp)
        .join(Application, FollowUp.application_id == Application.id)
        .filter(Application.company_id == company_id)
        .all()
    )

    # "Contacted before" warning (section 9 dedup, surfaced BEFORE drafting/sending)
    last_contacted = None
    for application in applications:
        if application.state in SENT_STATES and application.sent_at is not None:
            if last_contacted is None or application.sent_at > last_contacted.sent_at:
                last_contacted = application

    # Contact suggestions found on the company's own pages, minus already-added ones
    existing_emails = {c.email.strip().casefold() for c in contacts}
    existing_names = {c.name.strip().casefold() for c in contacts}
    suggested_contacts = [
        s
        for s in company.research.get("suggested_contacts", [])
        if s["name"].strip().casefold() not in existing_names
        and (not s.get("email") or s["email"].strip().casefold() not in existing_emails)
    ]

    return render(
        templates,
        request,
        "company_detail.html",
        {
            "company": company,
            "applications": applications,
            "contacts": contacts,
            "blocked": has_unconfirmed_hard_exclusion(company),
            "last_contacted": last_contacted,
            "last_contacted_days": _days_since(last_contacted.sent_at) if last_contacted else None,
            "suggested_contacts": suggested_contacts,
            "timeline": _build_timeline(applications, follow_ups),
        },
    )


@router.post("/research")
def run_research(company_id: int, db: Session = Depends(get_db), llm: LLMProvider = Depends(get_llm)):
    company = _company_or_404(db, company_id)
    research_company_safe(db, llm, company)
    return RedirectResponse(url=f"/companies/{company_id}", status_code=303)


@router.post("/details")
def update_details(
    company_id: int,
    contact_email: str = Form(""),
    notes: str = Form(""),
    why_interested: str = Form(""),
    location: str = Form(""),
    technopark: str = Form(""),
    db: Session = Depends(get_db),
):
    company = _company_or_404(db, company_id)
    company.contact_email = contact_email.strip() or None
    company.notes = notes.strip()
    company.why_interested = why_interested.strip()
    company.location = location.strip()
    company.technopark = technopark.strip()
    db.commit()
    return RedirectResponse(url=f"/companies/{company_id}", status_code=303)


@router.post("/delete")
def delete_company(company_id: int, db: Session = Depends(get_db)):
    """Owner-initiated purge - deletes the company along with its applications
    and contacts. Unlike the reject/regenerate flow, an explicit delete here is
    the owner choosing to remove the whole record, not just its current draft."""
    company = _company_or_404(db, company_id)
    db.query(Application).filter(Application.company_id == company_id).delete()
    db.query(Contact).filter(Contact.company_id == company_id).delete()
    db.delete(company)
    db.commit()
    return RedirectResponse(url="/companies", status_code=303)


@router.post("/confirm-exclusion")
def confirm_exclusion(company_id: int, db: Session = Depends(get_db)):
    company = _company_or_404(db, company_id)
    company.hard_exclusion_confirmed = True
    db.commit()
    return RedirectResponse(url=f"/companies/{company_id}", status_code=303)


@router.post("/draft")
def draft(
    company_id: int,
    target_position: str = Form(""),
    application_reason: str = Form(""),
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_llm),
):
    """Both fields optional: the dashboard's one-click draft posts neither; the
    company page's form lets the owner fix the position and/or write the reason
    for an open (no-posting) application."""
    company = _company_or_404(db, company_id)
    if has_unconfirmed_hard_exclusion(company):
        return RedirectResponse(url=f"/companies/{company_id}", status_code=303)
    application = create_draft_application(
        db, llm, company, target_position=target_position, application_reason=application_reason
    )
    return RedirectResponse(url=f"/applications/{application.id}", status_code=303)


@router.post("/contacts")
def add_contact(
    company_id: int,
    name: str = Form(...),
    role: str = Form(""),
    email: str = Form(...),
    source_url: str = Form(""),
    db: Session = Depends(get_db),
):
    _company_or_404(db, company_id)
    contact = Contact(
        company_id=company_id,
        name=name.strip(),
        role=role.strip(),
        email=email.strip(),
        source_url=source_url.strip() or None,
        added_manually=True,
    )
    db.add(contact)
    db.commit()
    return RedirectResponse(url=f"/companies/{company_id}", status_code=303)


@router.post("/contacts/suggested")
def add_suggested_contact(
    company_id: int,
    name: str = Form(...),
    role: str = Form(""),
    email: str = Form(""),
    source_url: str = Form(""),
    db: Session = Depends(get_db),
):
    """Owner confirmed a research-suggested person into a real Contact. Kept as
    an explicit confirm step - suggestions alone never become contacts."""
    _company_or_404(db, company_id)
    contact = Contact(
        company_id=company_id,
        name=name.strip(),
        role=role.strip(),
        email=email.strip(),
        source_url=source_url.strip() or None,
        added_manually=False,
    )
    db.add(contact)
    db.commit()
    return RedirectResponse(url=f"/companies/{company_id}", status_code=303)


@router.post("/contacts/{contact_id}/escalate")
def escalate_contact(
    company_id: int,
    contact_id: int,
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_llm),
):
    contact = db.get(Contact, contact_id)
    if contact is None or contact.company_id != company_id:
        return RedirectResponse(url=f"/companies/{company_id}", status_code=303)
    application = create_escalation_draft(db, llm, contact)
    return RedirectResponse(url=f"/applications/{application.id}", status_code=303)
