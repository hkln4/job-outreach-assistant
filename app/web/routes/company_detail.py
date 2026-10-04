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
from urllib.parse import urljoin

from app.applications.drafting import create_draft_application
from app.applications.escalation import create_escalation_draft
from app.applications.review import SENT_STATES
from app.companies.fetch import find_linkedin_url
from app.companies.linkedin_fetch import LINKEDIN_BASE
from app.companies.linkedin_workflow import linkedin_research_company_safe
from app.companies.workflow import (
    has_unconfirmed_hard_exclusion,
    has_usable_research,
    research_company_safe,
)
from app.config import get_settings
from app.db import get_db
from app.email_send.gmail_client import validate_email
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


def _suggested_people(company: Company, contacts: list[Contact]) -> list[dict]:
    """People to consider contacting, from both sources, minus already-added
    ones. The site and LinkedIn find different kinds of people (a named HR
    address on a contact page vs. a CTO on LinkedIn) but the owner does the
    same thing with both, so they share one table and one confirm step.

    LinkedIn people carry no email - LinkedIn never exposes one - so they are
    deduped by name alone, while site suggestions also dedupe on their address.
    """
    existing_emails = {c.email.strip().casefold() for c in contacts if c.email.strip()}
    existing_names = {c.name.strip().casefold() for c in contacts}

    people = []
    for suggestion in company.research.get("suggested_contacts", []) or []:
        name = (suggestion.get("name") or "").strip()
        email = (suggestion.get("email") or "").strip()
        if not name or name.casefold() in existing_names:
            continue
        if email and email.casefold() in existing_emails:
            continue
        people.append({**suggestion, "name": name, "email": email, "source": "site"})

    for person in company.linkedin_research.get("people", []) or []:
        name = (person.get("name") or "").strip()
        if not name or name.casefold() in existing_names:
            continue
        if any(p["name"].casefold() == name.casefold() for p in people):
            continue
        people.append(
            {
                "name": name,
                "role": (person.get("role") or "").strip(),
                "email": "",
                # Absolutised on the way in now, but research cached before
                # that fix stored bare "/in/slug/" paths, which would resolve
                # against this app instead of LinkedIn.
                "source_url": urljoin(LINKEDIN_BASE, person.get("profile_url") or ""),
                "source": "linkedin",
            }
        )
    return people


def _days_since(moment: datetime | None) -> int | None:
    if moment is None:
        return None
    if moment.tzinfo is None:  # SQLite drops tzinfo on read
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).days


@router.get("")
def company_detail(
    request: Request,
    company_id: int,
    linkedin_not_found: int = 0,
    db: Session = Depends(get_db),
):
    company = _company_or_404(db, company_id)
    applications = (
        db.query(Application)
        .filter(Application.company_id == company_id)
        .order_by(Application.id.desc())
        .all()
    )
    contacts = db.query(Contact).filter(Contact.company_id == company_id).order_by(Contact.id.desc()).all()

    # "Contacted before" note (section 9 dedup, surfaced BEFORE drafting/sending)
    last_contacted = None
    for application in applications:
        if application.state in SENT_STATES and application.sent_at is not None:
            if last_contacted is None or application.sent_at > last_contacted.sent_at:
                last_contacted = application

    return render(
        templates,
        request,
        "company_detail.html",
        {
            "company": company,
            "applications": applications,
            "contacts": contacts,
            "blocked": has_unconfirmed_hard_exclusion(company),
            "research_usable": has_usable_research(company),
            "last_contacted": last_contacted,
            "last_contacted_days": _days_since(last_contacted.sent_at) if last_contacted else None,
            "suggested_people": _suggested_people(company, contacts),
            "linkedin_enabled": get_settings().linkedin_research_enabled,
            "linkedin_not_found": bool(linkedin_not_found),
        },
    )


@router.post("/research")
def run_research(company_id: int, db: Session = Depends(get_db), llm: LLMProvider = Depends(get_llm)):
    company = _company_or_404(db, company_id)
    research_company_safe(db, llm, company)
    return RedirectResponse(url=f"/companies/{company_id}", status_code=303)


@router.post("/linkedin-research")
def run_linkedin_research(
    company_id: int, db: Session = Depends(get_db), llm: LLMProvider = Depends(get_llm)
):
    """One company per explicit click - never on create, never in a bulk loop
    (CLAUDE.md section 9, LinkedIn research rule)."""
    company = _company_or_404(db, company_id)
    if get_settings().linkedin_research_enabled:
        linkedin_research_company_safe(db, llm, company)
    return RedirectResponse(url=f"/companies/{company_id}", status_code=303)


@router.post("/linkedin-url")
def set_linkedin_url(
    company_id: int,
    linkedin_url: str = Form(""),
    db: Session = Depends(get_db),
):
    """Paste the company's LinkedIn page straight into the LinkedIn card,
    without going through the whole details form further down the page."""
    company = _company_or_404(db, company_id)
    company.linkedin_url = linkedin_url.strip() or None
    db.commit()
    return RedirectResponse(url=f"/companies/{company_id}", status_code=303)


@router.post("/linkedin-url/find")
def find_linkedin_url_on_site(company_id: int, db: Session = Depends(get_db)):
    """Look for the LinkedIn link on the company's own site.

    Companies added before LinkedIn research existed have no linkedin_url, and
    re-running the full research to pick one up would cost an LLM call per
    company. This just re-reads their site's social icons - seconds, no LLM,
    no LinkedIn traffic. A miss leaves the field empty for the owner to fill.
    """
    company = _company_or_404(db, company_id)
    found = find_linkedin_url(company.website_url or "")
    if not found:
        return RedirectResponse(url=f"/companies/{company_id}?linkedin_not_found=1", status_code=303)
    company.linkedin_url = found
    db.commit()
    return RedirectResponse(url=f"/companies/{company_id}", status_code=303)


@router.post("/details")
def update_details(
    company_id: int,
    contact_email: str = Form(""),
    notes: str = Form(""),
    why_interested: str = Form(""),
    location: str = Form(""),
    technopark: str = Form(""),
    linkedin_url: str | None = Form(None),
    db: Session = Depends(get_db),
):
    company = _company_or_404(db, company_id)
    company.contact_email = contact_email.strip() or None
    # None means the form didn't carry the field at all (LinkedIn research off),
    # which must leave a stored URL alone; an empty string is the owner clearing it.
    if linkedin_url is not None:
        company.linkedin_url = linkedin_url.strip() or None
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


@router.post("/contacts/{contact_id}/email")
def set_contact_email(
    company_id: int,
    contact_id: int,
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    """Fill in the address for a contact that arrived without one (LinkedIn
    gives names and roles, never emails). Invalid input is ignored rather than
    saved - an unsendable address is worse than an empty one."""
    contact = db.get(Contact, contact_id)
    if contact is not None and contact.company_id == company_id and validate_email(email.strip()):
        contact.email = email.strip()
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
    if contact is None or contact.company_id != company_id or not contact.email.strip():
        return RedirectResponse(url=f"/companies/{company_id}", status_code=303)
    application = create_escalation_draft(db, llm, contact)
    return RedirectResponse(url=f"/applications/{application.id}", status_code=303)
