"""Preferences & Constraints CRUD (CLAUDE.md section 4)."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import EMAIL_LANGUAGES, OwnerSettings, Preference, PreferenceType, get_email_language
from app.web.render import render

router = APIRouter(prefix="/preferences", tags=["preferences"])
templates = Jinja2Templates(directory="app/web/templates")


@router.get("")
def list_preferences(request: Request, db: Session = Depends(get_db)):
    preferences = db.query(Preference).order_by(Preference.id.desc()).all()
    return render(
        templates,
        request,
        "preferences.html",
        {
            "preferences": preferences,
            "email_language": get_email_language(db),
            "email_languages": list(EMAIL_LANGUAGES),
        },
    )


@router.post("/email-language")
def set_email_language(email_language: str = Form(""), db: Session = Depends(get_db)):
    """Language for outgoing letters (drafts and escalations); empty = no preference."""
    settings = db.query(OwnerSettings).first()
    if settings is None:
        settings = OwnerSettings()
        db.add(settings)
    settings.email_language = email_language if email_language in EMAIL_LANGUAGES else ""
    db.commit()
    return RedirectResponse(url="/preferences", status_code=303)


@router.post("")
def create_preference(
    description: str = Form(...),
    type: str = Form(...),
    signals: str = Form(""),
    db: Session = Depends(get_db),
):
    signal_list = [s.strip() for s in signals.split(",") if s.strip()]
    pref = Preference(description=description.strip(), type=PreferenceType(type), signals=signal_list)
    db.add(pref)
    db.commit()
    return RedirectResponse(url="/preferences", status_code=303)


@router.post("/{preference_id}/delete")
def delete_preference(preference_id: int, db: Session = Depends(get_db)):
    pref = db.get(Preference, preference_id)
    if pref is not None:
        db.delete(pref)
        db.commit()
    return RedirectResponse(url="/preferences", status_code=303)
