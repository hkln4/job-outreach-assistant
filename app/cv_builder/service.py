"""Generated CV lifecycle: assemble a selection for an application -> the
owner previews and edits it -> render + ATS check -> approve -> attached.
Edits made in the preview apply to this CV only unless the owner chooses
"save to bank"."""
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.cv_builder.ats import check
from app.cv_builder.assemble import assemble
from app.cv_builder.bank import edit_bullet
from app.cv_builder.render import render
from app.models import Application, Bullet, GeneratedCV
from app.postings.gates import detect_language


def create_for_application(db: Session, application: Application) -> GeneratedCV:
    posting = application.job_posting
    language = (posting.language if posting and posting.language in ("tr", "en") else "") or "en"
    generated = GeneratedCV(
        application_id=application.id,
        job_posting_id=posting.id if posting else None,
        selection=assemble(db, posting, application.company),
        language=language,
    )
    db.add(generated)
    db.commit()
    return generated


def update_selection(db: Session, generated: GeneratedCV, form: dict) -> None:
    """Preview edits: chosen projects, bullets, skills, certificates, bullet
    text for this CV (or saved to the bank)."""
    selection = dict(generated.selection or {})
    if "project_ids" in form:
        selection["project_ids"] = [int(i) for i in form["project_ids"]]
    if "bullet_ids" in form:
        selection["bullet_ids"] = {pid: [int(i) for i in ids] for pid, ids in form["bullet_ids"].items()}
    if "skills" in form:
        selection["skills"] = form["skills"]
    if "certificates" in form:
        selection["certificates"] = bool(form["certificates"])
    overrides = dict(selection.get("bullet_overrides") or {})
    for bullet_id, text in (form.get("bullet_text") or {}).items():
        bullet = db.get(Bullet, int(bullet_id))
        if bullet is None or not text.strip():
            continue
        if bullet_id in (form.get("save_to_bank") or set()):
            edit_bullet(db, bullet, text)
            overrides.pop(bullet_id, None)
        elif text.strip() != bullet.text:
            overrides[bullet_id] = text.strip()
    selection["bullet_overrides"] = overrides
    if form.get("language") in ("tr", "en"):
        generated.language = form["language"]
    generated.selection = selection
    generated.status = "draft"
    db.commit()


def render_generated(db: Session, generated: GeneratedCV) -> GeneratedCV:
    pdf, data = render(db, generated)
    report = check(pdf, data, (generated.selection or {}).get("keywords") or [])
    out_dir = Path(get_settings().generated_cvs_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(pdf).hexdigest()
    path = out_dir / f"generated_{generated.id}_{digest[:12]}.pdf"
    path.write_bytes(pdf)
    generated.pdf_path = str(path)
    generated.sha256 = digest
    generated.ats_report = report
    generated.keyword_report = report["keyword_report"]
    generated.status = "draft"
    db.commit()
    return generated


def approve(db: Session, generated: GeneratedCV) -> None:
    """Blocked while the ATS check fails or the PDF is stale."""
    if not generated.pdf_path or not (generated.ats_report or {}).get("passed"):
        raise ValueError("Render the CV and pass the ATS check before approving it.")
    generated.status = "approved"
    generated.approved_at = datetime.now(timezone.utc)
    if generated.application_id:
        application = db.get(Application, generated.application_id)
        application.generated_cv_id = generated.id
    db.commit()


def language_of(text: str) -> str:
    return detect_language(text) or "en"
