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
from app.web.files import safe_component
from app.web.render import render

router = APIRouter(prefix="/cvs", tags=["cvs"])
templates = Jinja2Templates(directory="app/web/templates")

CV_VARIANTS_DIR = "data/cv_variants"


@router.get("")
def list_cvs(request: Request, db: Session = Depends(get_db)):
    cvs = db.query(CV).order_by(CV.id.desc()).all()
    return render(templates, request, "cvs.html", {"cvs": cvs})


@router.post("")
def create_cv(
    name: str = Form(...),
    target_role: str = Form(""),
    notes: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    os.makedirs(CV_VARIANTS_DIR, exist_ok=True)
    safe_name = f"{safe_component(name)}{Path(file.filename).suffix}"
    file_path = os.path.join(CV_VARIANTS_DIR, safe_name)
    with open(file_path, "wb") as f:
        f.write(file.file.read())

    cv = CV(name=name.strip(), target_role=target_role.strip(), file_path=file_path, notes=notes.strip())
    db.add(cv)
    db.commit()
    return RedirectResponse(url="/cvs", status_code=303)


@router.post("/{cv_id}/delete")
def delete_cv(cv_id: int, db: Session = Depends(get_db)):
    cv = db.get(CV, cv_id)
    if cv is not None:
        if cv.file_path and os.path.exists(cv.file_path):
            os.remove(cv.file_path)
        db.delete(cv)
        db.commit()
    return RedirectResponse(url="/cvs", status_code=303)
