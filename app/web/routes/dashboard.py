"""Landing page: counts per state, so the owner can see where things stand at a glance."""
from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Application, Company, CompanyState

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="app/web/templates")


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    company_counts = dict(
        db.query(Company.state, func.count(Company.id)).group_by(Company.state).all()
    )
    total_companies = sum(company_counts.values())
    total_applications = db.query(func.count(Application.id)).scalar() or 0

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "total_companies": total_companies,
            "applicable_count": company_counts.get(CompanyState.APPLICABLE, 0),
            "total_applications": total_applications,
        },
    )
