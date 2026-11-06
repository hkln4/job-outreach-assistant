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
from app.companies.workflow import has_unconfirmed_hard_exclusion, has_usable_research
from app.db import get_db
from app.llm.call_log import cost_summary
from app.models import (
    CV, Application, ApplicationState, CandidateStatus, Company, CompanyCandidate, CompanyState, JobPosting, PostingStatus,
    Preference, Profile,
)
from app.postings.filter import check_posting, owner_filter

# How many rows of one kind the "today" list shows before "+N more".
PER_KIND = 6
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
def dashboard(request: Request, checked: int = -1, replies: int = 0, bounces: int = 0, db: Session = Depends(get_db)):
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
    # Research that fetched pages but extracted no text belongs in "needs
    # research", not "ready to draft" - there is nothing to write a grounded
    # letter from, so offering the draft button only leads to a failure.
    needs_research = [c for c in applicable_companies if not has_usable_research(c)]
    blocked = [c for c in applicable_companies if has_unconfirmed_hard_exclusion(c)]
    blocked_ids = {c.id for c in blocked}
    ready_to_draft = [
        c
        for c in applicable_companies
        if has_usable_research(c) and c.id not in blocked_ids and c.id not in applied_company_ids
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

    rules = owner_filter(db)  # the same filter the postings page applies
    new_postings = [
        p for p in db.query(JobPosting).filter(JobPosting.status == PostingStatus.NEW, JobPosting.inbox.is_(False))
        .order_by(JobPosting.id.desc()).limit(100).all()
        if check_posting(rules, p)["match"] and (p.fit or {}).get("verdict") not in ("weak", "blocked")
    ]
    inbox_count = sum(
        1 for p in db.query(JobPosting).filter(JobPosting.status == PostingStatus.NEW, JobPosting.inbox.is_(True)).all()
        if check_posting(rules, p)["match"]
    )
    candidates_waiting = db.query(func.count(CompanyCandidate.id)).filter(CompanyCandidate.status == CandidateStatus.PENDING).scalar() or 0
    month_costs = cost_summary(db)["by_month"].get(datetime.now(timezone.utc).strftime("%Y-%m"))

    todo = today_list(pending_review, overdue, new_postings, ready_to_draft, needs_research, blocked, inbox_count)
    return render(
        templates,
        request,
        "dashboard.html",
        {
            "todo": todo,
            "candidates_waiting": candidates_waiting,
            "month_cost": month_costs["cost"] if month_costs else 0.0,
            "new_postings": new_postings,
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
            "check_summary": {"checked": checked, "replies": replies, "bounces": bounces} if checked >= 0 else None,
        },
    )


def today_list(pending_review, overdue, new_postings, ready_to_draft, needs_research, blocked, inbox_count: int = 0) -> list[dict]:
    """One list of what to do now, most urgent first: drafts waiting for you,
    applications past the reply threshold, new postings that fit, companies
    ready for a letter, then companies that need research or a decision."""
    groups = [
        ("review", [{"title": a.company.name, "meta": a.target_position, "href": f"/applications/{a.id}", "link": True} for a in pending_review]),
        ("overdue", [{"title": r["application"].company.name, "meta": f'{r["days_waiting"]}d' if r["days_waiting"] is not None else "",
                      "href": f'/applications/{r["application"].id}', "action": f'/applications/{r["application"].id}/follow-ups/draft',
                      "loading": "drafting"} for r in overdue]),
        ("inbox", [{"title": "", "meta": "", "href": "/postings?view=inbox", "link": True}] if inbox_count else []),
        ("posting", [{"title": p.title or "—", "meta": p.company.name if p.company else p.company_name, "href": f"/postings/{p.id}",
                      "link": True, "badge": (p.fit or {}).get("verdict")} for p in new_postings]),
        ("draft", [{"title": c.name, "meta": "", "href": f"/companies/{c.id}", "action": f"/companies/{c.id}/draft", "loading": "drafting"} for c in ready_to_draft]),
        ("research", [{"title": c.name, "meta": "", "href": f"/companies/{c.id}", "action": f"/companies/{c.id}/research", "loading": "researching"} for c in needs_research]),
        ("blocked", [{"title": c.name, "meta": "", "href": f"/companies/{c.id}", "link": True} for c in blocked]),
    ]
    return [
        {"kind": kind, "rows": rows[:PER_KIND], "more": max(0, len(rows) - PER_KIND), "count": inbox_count if kind == "inbox" else len(rows)}
        for kind, rows in groups
        if rows
    ]


@router.post("/check-replies")
def check_replies(db: Session = Depends(get_db)):
    """Bulk owner-triggered reply check over every AWAITING_REPLY application."""
    summary = check_all_awaiting(db)
    return RedirectResponse(
        url=f"/?checked={summary['checked']}&replies={summary['replies']}&bounces={summary['bounces']}", status_code=303
    )
