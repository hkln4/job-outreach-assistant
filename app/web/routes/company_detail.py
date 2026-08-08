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

from app.applications.drafting import create_draft_application
from app.applications.escalation import create_escalation_draft
from app.companies.workflow import has_unconfirmed_hard_exclusion, research_company_safe
from app.db import get_db
from app.llm.base import LLMProvider
from app.models import Application, Company, Contact
from app.web.deps import get_llm
from app.web.render import render

router = APIRouter(prefix="/companies/{company_id}", tags=["company-detail"])
templates = Jinja2Templates(directory="app/web/templates")


def _company_or_404(db: Session, company_id: int) -> Company:
    company = db.get(Company, company_id)
    if company is None:
        raise ValueError(f"Company {company_id} not found")
    return company


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
    return render(
        templates,
        request,
        "company_detail.html",
        {
            "company": company,
            "applications": applications,
            "contacts": contacts,
            "blocked": has_unconfirmed_hard_exclusion(company),
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
    db: Session = Depends(get_db),
):
    company = _company_or_404(db, company_id)
    company.contact_email = contact_email.strip() or None
    company.notes = notes.strip()
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
def draft(company_id: int, db: Session = Depends(get_db), llm: LLMProvider = Depends(get_llm)):
    company = _company_or_404(db, company_id)
    if has_unconfirmed_hard_exclusion(company):
        return RedirectResponse(url=f"/companies/{company_id}", status_code=303)
    application = create_draft_application(db, llm, company)
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
