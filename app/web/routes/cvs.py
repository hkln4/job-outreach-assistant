"""CV variant management (section 6: CV entity) - one CV list serves two
purposes: the exact file attached to outgoing applications, AND the source
Profile Builder ingests from (see app/web/routes/profile.py). A file is
required so both uses always have something to work with."""
import os
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import CV
from app.web.files import unique_stored_filename
from app.web.render import render

router = APIRouter(prefix="/cvs", tags=["cvs"])
templates = Jinja2Templates(directory="app/web/templates")

CV_VARIANTS_DIR = "data/cv_variants"
# What cv_parser can read and what a recruiter expects as an attachment.
ALLOWED_CV_SUFFIXES = (".pdf", ".docx")
MAX_CV_BYTES = 10 * 1024 * 1024
UPLOAD_ERRORS = ("type", "size", "empty")


@router.get("")
def list_cvs(request: Request, error: str = "", db: Session = Depends(get_db)):
    cvs = db.query(CV).order_by(CV.id.desc()).all()
    return render(
        templates, request, "cvs.html", {"cvs": cvs, "error": error if error in UPLOAD_ERRORS else ""}
    )


@router.post("")
def create_cv(
    name: str = Form(...),
    target_role: str = Form(""),
    notes: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_CV_SUFFIXES:
        return RedirectResponse(url="/cvs?error=type", status_code=303)
    content = file.file.read(MAX_CV_BYTES + 1)
    if len(content) > MAX_CV_BYTES:
        return RedirectResponse(url="/cvs?error=size", status_code=303)
    if not content:
        return RedirectResponse(url="/cvs?error=empty", status_code=303)

    os.makedirs(CV_VARIANTS_DIR, exist_ok=True)
    file_path = os.path.join(CV_VARIANTS_DIR, unique_stored_filename(name, suffix))
    with open(file_path, "wb") as f:
        f.write(content)

    cv = CV(name=name.strip(), target_role=target_role.strip(), file_path=file_path, notes=notes.strip())
    db.add(cv)
    db.commit()
    return RedirectResponse(url="/cvs", status_code=303)


@router.post("/{cv_id}/delete")
def delete_cv(cv_id: int, db: Session = Depends(get_db)):
    cv = db.get(CV, cv_id)
    if cv is not None:
        # CVs uploaded before names were unique can share one file - only
        # remove it once nothing else points at it.
        shared = db.query(CV).filter(CV.file_path == cv.file_path, CV.id != cv.id).count()
        if cv.file_path and not shared and os.path.exists(cv.file_path):
            os.remove(cv.file_path)
        db.delete(cv)
        db.commit()
    return RedirectResponse(url="/cvs", status_code=303)
