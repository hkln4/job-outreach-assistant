"""Funnel page (roadmap phase 7): reply and interview rates by source,
channel, CV, language and angle. Plain HTML + inline SVG bars."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.applications.outcomes import DIMENSIONS, funnel, record_outcome
from app.applications.review import StateTransitionError
from app.db import get_db
from app.llm.call_log import cost_summary
from app.models import Application, ApplicationState
from app.web.errors import NotFoundError
from app.web.render import render

router = APIRouter(tags=["funnel"])
templates = Jinja2Templates(directory="app/web/templates")


@router.get("/funnel")
def funnel_page(request: Request, db: Session = Depends(get_db)):
    costs = cost_summary(db)
    return render(templates, request, "funnel.html", {"data": funnel(db), "dimensions": DIMENSIONS, "costs": costs})


@router.post("/applications/{application_id}/outcome")
def outcome(application_id: int, state: str = Form(...), note: str = Form(""), db: Session = Depends(get_db)):
    application = db.get(Application, application_id)
    if application is None:
        raise NotFoundError(f"Application {application_id} not found")
    try:
        record_outcome(db, application, ApplicationState(state), note)
    except (StateTransitionError, ValueError):
        return RedirectResponse(url=f"/applications/{application_id}?blocked=1", status_code=303)
    return RedirectResponse(url=f"/applications/{application_id}", status_code=303)
