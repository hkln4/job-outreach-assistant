"""LLM cost page (CLAUDE.md section 8): what every model call cost, per month,
per purpose, per company and per application - read from LLMCallLog."""
from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.llm.call_log import cost_summary
from app.models import Application, Company
from app.web.render import render

router = APIRouter(tags=["costs"])
templates = Jinja2Templates(directory="app/web/templates")

TOP_N = 20


@router.get("/costs")
def costs(request: Request, db: Session = Depends(get_db)):
    summary = cost_summary(db)
    company_names = {c.id: c.name for c in db.query(Company).filter(Company.id.in_(list(summary["by_company"]))).all()}
    applications = {
        a.id: a for a in db.query(Application).filter(Application.id.in_(list(summary["by_application"]))).all()
    }
    top_companies = sorted(summary["by_company"].items(), key=lambda kv: -kv[1]["cost"])[:TOP_N]
    top_applications = sorted(summary["by_application"].items(), key=lambda kv: -kv[1]["cost"])[:TOP_N]
    return render(
        templates,
        request,
        "costs.html",
        {
            "summary": summary,
            "top_companies": [(company_names.get(cid, f"#{cid}"), cid, data) for cid, data in top_companies],
            "top_applications": [(applications.get(aid), aid, data) for aid, data in top_applications],
        },
    )
