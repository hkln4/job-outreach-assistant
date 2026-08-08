"""Landing page: the actual action queue for the section-5 workflow (who needs
review, who needs a draft, who needs research) plus a getting-set-up checklist
that gets out of the way once done. This is the one page the owner should be
able to work entirely from - no digging through nested pages to find out what
to do next."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.applications.reply_check import check_all_awaiting, overdue_applications
from app.companies.workflow import has_unconfirmed_hard_exclusion
from app.db import get_db
from app.models import CV, Application, ApplicationState, Company, CompanyState, Preference, Profile
from app.web.render import render

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="app/web/templates")


def _days_since(moment: datetime | None) -> int | None:
    if moment is None:
        return None
    if moment.tzinfo is None:  # SQLite drops tzinfo on read
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).days


@router.get("/")
def dashboard(request: Request, checked: int = -1, replies: int = 0, db: Session = Depends(get_db)):
    company_counts = dict(
        db.query(Company.state, func.count(Company.id)).group_by(Company.state).all()
    )
    total_companies = sum(company_counts.values())
    total_applications = db.query(func.count(Application.id)).scalar() or 0

    profile = db.query(Profile).first()

    checklist = [
        {"key": "cv", "done": db.query(CV).first() is not None, "href": "/cvs"},
        {"key": "preferences", "done": db.query(Preference).first() is not None, "href": "/preferences"},
        {"key": "profile", "done": bool(profile and profile.summary), "href": "/profile"},
        {"key": "company", "done": total_companies > 0, "href": "/companies"},
    ]
    setup_done = all(step["done"] for step in checklist)

    pending_review = (
        db.query(Application)
        .filter(Application.state == ApplicationState.PENDING_APPROVAL)
        .order_by(Application.last_action_at.desc())
        .all()
    )
    applied_company_ids = {a.company_id for a in db.query(Application.company_id).distinct()}

    applicable_companies = (
        db.query(Company).filter(Company.state == CompanyState.APPLICABLE).order_by(Company.id.desc()).all()
    )
    needs_research = [c for c in applicable_companies if not c.research.get("pages")]
    blocked = [c for c in applicable_companies if has_unconfirmed_hard_exclusion(c)]
    blocked_ids = {c.id for c in blocked}
    ready_to_draft = [
        c
        for c in applicable_companies
        if c.research.get("pages") and c.id not in blocked_ids and c.id not in applied_company_ids
    ]

    awaiting_count = (
        db.query(func.count(Application.id))
        .filter(Application.state == ApplicationState.AWAITING_REPLY, Application.gmail_thread_id.isnot(None))
        .scalar()
        or 0
    )
    overdue = [
        {"application": application, "days_waiting": _days_since(application.sent_at)}
        for application in overdue_applications(db)
    ]

    return render(
        templates,
        request,
        "dashboard.html",
        {
            "total_companies": total_companies,
            "applicable_count": company_counts.get(CompanyState.APPLICABLE, 0),
            "total_applications": total_applications,
            "checklist": checklist,
            "setup_done": setup_done,
            "pending_review": pending_review,
            "needs_research": needs_research,
            "ready_to_draft": ready_to_draft,
            "blocked": blocked,
            "awaiting_count": awaiting_count,
            "overdue": overdue,
            # bulk reply-check result banner (checked == -1 means "no check just ran")
            "check_summary": {"checked": checked, "replies": replies} if checked >= 0 else None,
        },
    )


@router.post("/check-replies")
def check_replies(db: Session = Depends(get_db)):
    """Bulk owner-triggered reply check over every AWAITING_REPLY application."""
    summary = check_all_awaiting(db)
    return RedirectResponse(
        url=f"/?checked={summary['checked']}&replies={summary['replies']}", status_code=303
    )
