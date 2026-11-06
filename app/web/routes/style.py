"""Style page (roadmap phase 4): the letters marked as good examples and the
standing rules promoted from feedback - all visible and editable, since both
go into every drafting prompt."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import StyleExample, StyleNote
from app.web.render import render

router = APIRouter(prefix="/style", tags=["style"])
templates = Jinja2Templates(directory="app/web/templates")


@router.get("")
def style_page(request: Request, db: Session = Depends(get_db)):
    return render(
        templates,
        request,
        "style.html",
        {
            "notes": db.query(StyleNote).order_by(StyleNote.id).all(),
            "examples": db.query(StyleExample).order_by(StyleExample.id.desc()).all(),
        },
    )


@router.post("/notes")
def add_note(text: str = Form(...), db: Session = Depends(get_db)):
    if text.strip():
        db.add(StyleNote(text=text.strip()))
        db.commit()
    return RedirectResponse(url="/preferences#style-section", status_code=303)


@router.post("/notes/{note_id}")
def update_note(note_id: int, text: str = Form(""), action: str = Form("save"), db: Session = Depends(get_db)):
    note = db.get(StyleNote, note_id)
    if note is not None:
        if action == "delete":
            db.delete(note)
        elif action == "toggle":
            note.active = not note.active
        elif text.strip():
            note.text = text.strip()
        db.commit()
    return RedirectResponse(url="/preferences#style-section", status_code=303)


@router.post("/examples/{example_id}/delete")
def delete_example(example_id: int, db: Session = Depends(get_db)):
    example = db.get(StyleExample, example_id)
    if example is not None:
        db.delete(example)
        db.commit()
    return RedirectResponse(url="/preferences#style-section", status_code=303)
