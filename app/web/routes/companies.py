"""
Company list CRUD (CLAUDE.md section 5 step 1): the owner manually curates target
companies and vets them. The system later acts only on state == APPLICABLE.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Company, CompanyState

router = APIRouter(prefix="/companies", tags=["companies"])
templates = Jinja2Templates(directory="app/web/templates")


@router.get("")
def list_companies(request: Request, db: Session = Depends(get_db)):
    companies = db.query(Company).order_by(Company.created_at.desc()).all()
    return templates.TemplateResponse(
        request, "companies.html", {"companies": companies, "states": list(CompanyState)}
    )


@router.post("")
def create_company(
    name: str = Form(...),
    website_url: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    company = Company(
        name=name.strip(),
        website_url=website_url.strip() or None,
        notes=notes.strip(),
        state=CompanyState.APPLICABLE,
    )
    db.add(company)
    db.commit()
    return RedirectResponse(url="/companies", status_code=303)


@router.post("/{company_id}/state")
def update_company_state(company_id: int, state: str = Form(...), db: Session = Depends(get_db)):
    company = db.get(Company, company_id)
    if company is not None:
        company.state = CompanyState(state)
        db.commit()
    return RedirectResponse(url="/companies", status_code=303)
