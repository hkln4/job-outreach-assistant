"""
Company list CRUD (CLAUDE.md section 5 step 1): the owner manually curates target
companies and vets them. The system later acts only on state == APPLICABLE.

Research (section 5 step 2) runs automatically right after a company with a
website is added, so the owner lands on the company page with findings already
there instead of having to trigger it as a separate manual step.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.applications.review import SENT_STATES
from app.companies.workflow import research_company_safe
from app.db import get_db
from app.llm.base import LLMProvider
from app.models import Application, Company, CompanyState
from app.web.deps import get_llm
from app.web.render import render

router = APIRouter(prefix="/companies", tags=["companies"])
templates = Jinja2Templates(directory="app/web/templates")


@router.get("")
def list_companies(
    request: Request,
    technopark: str = "",
    uncontacted: int = 0,
    db: Session = Depends(get_db),
):
    """Company list with the outreach-CRM filters: by technopark and by
    "never actually emailed yet" (e.g. ODTÜ Teknokent companies not contacted)."""
    query = db.query(Company)
    if technopark:
        query = query.filter(Company.technopark == technopark)
    companies = query.order_by(Company.created_at.desc()).all()

    contacted_ids = {
        row[0]
        for row in db.query(Application.company_id).filter(Application.state.in_(SENT_STATES)).distinct()
    }
    if uncontacted:
        companies = [c for c in companies if c.id not in contacted_ids]

    technoparks = sorted({c.technopark for c in db.query(Company).all() if c.technopark})
    return render(
        templates,
        request,
        "companies.html",
        {
            "companies": companies,
            "states": list(CompanyState),
            "contacted_ids": contacted_ids,
            "technoparks": technoparks,
            "filter_technopark": technopark,
            "filter_uncontacted": bool(uncontacted),
        },
    )


@router.post("")
def create_company(
    name: str = Form(...),
    website_url: str = Form(""),
    contact_email: str = Form(""),
    notes: str = Form(""),
    location: str = Form(""),
    technopark: str = Form(""),
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_llm),
):
    company = Company(
        name=name.strip(),
        website_url=website_url.strip() or None,
        contact_email=contact_email.strip() or None,
        notes=notes.strip(),
        location=location.strip(),
        technopark=technopark.strip(),
        state=CompanyState.APPLICABLE,
    )
    db.add(company)
    db.commit()
    db.refresh(company)

    if company.website_url:
        research_company_safe(db, llm, company)

    return RedirectResponse(url=f"/companies/{company.id}", status_code=303)


@router.post("/{company_id}/state")
def update_company_state(company_id: int, state: str = Form(...), db: Session = Depends(get_db)):
    company = db.get(Company, company_id)
    if company is not None:
        company.state = CompanyState(state)
        db.commit()
    return RedirectResponse(url="/companies", status_code=303)
