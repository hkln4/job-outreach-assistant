"""
Company list CRUD (CLAUDE.md section 5 step 1): the owner manually curates target
companies and vets them. The system later acts only on state == APPLICABLE.

Research (section 5 step 2) runs automatically right after a company with a
website is added, so the owner lands on the company page with findings already
there instead of having to trigger it as a separate manual step.

Adding is fetch-driven from the page's JS (single and bulk): the POST returns
JSON + a rendered table row, the page stays put and shows per-company progress
instead of redirecting away mid-research. The redirect branch is kept as a
no-JS fallback.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.applications.review import SENT_STATES
from app.companies.bulk_parse import parse_bulk_text
from app.companies.dedup import find_existing_company
from app.companies.workflow import research_company_safe
from app.db import get_db
from app.llm.base import LLMProvider
from app.models import Application, Company, CompanyState
from app.web.deps import get_llm
from app.web.i18n import get_lang, translator
from app.web.render import render

router = APIRouter(prefix="/companies", tags=["companies"])
templates = Jinja2Templates(directory="app/web/templates")


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


def _render_company_row(request: Request, company: Company) -> str:
    return templates.get_template("_company_row.html").render(
        company=company,
        states=list(CompanyState),
        contacted_ids=set(),
        t=translator(get_lang(request)),
    )


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
    request: Request,
    name: str = Form(...),
    website_url: str = Form(""),
    contact_email: str = Form(""),
    notes: str = Form(""),
    location: str = Form(""),
    technopark: str = Form(""),
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_llm),
):
    name = name.strip()
    website_url = website_url.strip() or None

    existing = find_existing_company(db, name, website_url)
    if existing is not None:
        if _wants_json(request):
            return JSONResponse(
                {"status": "duplicate", "company": {"id": existing.id, "name": existing.name}},
                status_code=409,
            )
        return RedirectResponse(url=f"/companies/{existing.id}", status_code=303)

    company = Company(
        name=name,
        website_url=website_url,
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

    if _wants_json(request):
        return JSONResponse(
            {
                "status": "created",
                "company": {"id": company.id, "name": company.name},
                "research_error": (company.research or {}).get("error", ""),
                "row_html": _render_company_row(request, company),
            }
        )
    return RedirectResponse(url="/companies", status_code=303)


@router.post("/bulk/parse")
def bulk_parse(text: str = Form(""), db: Session = Depends(get_db)):
    """Parse the pasted company list into rows and pre-check each against the
    DB, so the owner sees exactly what would be added (and what's flagged as
    duplicate/unusable) before any research call is spent."""
    rows = parse_bulk_text(text)
    for row in rows:
        if not row["error"] and find_existing_company(db, row["name"], row["website_url"]) is not None:
            row["error"] = "already_exists"
    return JSONResponse({"rows": rows})


@router.post("/{company_id}/state")
def update_company_state(company_id: int, state: str = Form(...), db: Session = Depends(get_db)):
    company = db.get(Company, company_id)
    if company is not None:
        company.state = CompanyState(state)
        db.commit()
    return RedirectResponse(url="/companies", status_code=303)
