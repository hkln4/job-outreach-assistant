"""Profile Builder web UI (CLAUDE.md section 3): ingest sources, view the
coverage ledger, run stage-2 synthesis, and review/edit the resulting Profile
before it's treated as the master profile.

CVs are NOT uploaded here - they live in the CVs tab (one CV entity serves both
purposes: attached to outgoing applications AND ingested here as a profile
source), so there's only ever one place to manage CV files."""
import os

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.llm.base import LLMProvider
from app.models import CV, Profile, ProfileItem
from app.profile_builder.coverage import ledger_summary
from app.profile_builder.cv_parser import ingest_cv
from app.profile_builder.github_ingest import ingest_repo, list_candidate_repos
from app.profile_builder.linkedin_ingest import ingest_linkedin_export
from app.profile_builder.site_ingest import ingest_site
from app.profile_builder.synthesis import synthesize_profile
from app.web.deps import get_llm
from app.web.files import safe_uploaded_filename
from app.web.render import render

router = APIRouter(prefix="/profile", tags=["profile"])
templates = Jinja2Templates(directory="app/web/templates")

LINKEDIN_SOURCES_DIR = "data/profile_sources/linkedin"


def _ingested_cv_paths(db: Session) -> set[str]:
    return {
        item.source_ref
        for item in db.query(ProfileItem).filter(ProfileItem.source_type == "cv").all()
    }


@router.get("")
def profile_home(request: Request, db: Session = Depends(get_db)):
    profile = db.query(Profile).first()
    items = db.query(ProfileItem).order_by(ProfileItem.created_at.desc()).all()
    ingested_paths = _ingested_cv_paths(db)
    cvs = db.query(CV).filter(CV.file_path != "").all()
    pending_cvs = [cv for cv in cvs if cv.file_path not in ingested_paths]
    return render(
        templates,
        request,
        "profile.html",
        {
            "profile": profile,
            "items": items,
            "ledger": ledger_summary(db),
            "pending_cvs": pending_cvs,
            "has_any_cv": bool(cvs),
        },
    )


@router.post("/items/{item_id}/delete")
def delete_profile_item(item_id: int, db: Session = Depends(get_db)):
    item = db.get(ProfileItem, item_id)
    if item is not None:
        db.delete(item)
        db.commit()
    return RedirectResponse(url="/profile", status_code=303)


@router.post("/items/{item_id}/retry")
def retry_profile_item(item_id: int, db: Session = Depends(get_db), llm: LLMProvider = Depends(get_llm)):
    """Re-runs extraction for one item using its existing source - for items
    that came back needs_review because the LLM call itself failed (rate
    limit, no credit, etc.), not because the source was genuinely thin."""
    item = db.get(ProfileItem, item_id)
    if item is None:
        return RedirectResponse(url="/profile", status_code=303)

    source_type, source_ref = item.source_type, item.source_ref
    db.delete(item)
    db.commit()

    if source_type == "cv":
        ingest_cv(db, llm, source_ref)
    elif source_type == "linkedin":
        ingest_linkedin_export(db, llm, source_ref)
    elif source_type == "site":
        ingest_site(db, llm, source_ref)
    elif source_type == "github_repo":
        settings = get_settings()
        ingest_repo(db, llm, settings.github_token, source_ref)

    return RedirectResponse(url="/profile", status_code=303)


@router.post("/cv/ingest/{cv_id}")
def ingest_cv_from_tab(
    cv_id: int,
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_llm),
):
    cv = db.get(CV, cv_id)
    if cv is not None and cv.file_path:
        ingest_cv(db, llm, cv.file_path)
    return RedirectResponse(url="/profile", status_code=303)


@router.post("/linkedin/upload")
def upload_linkedin(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_llm),
):
    os.makedirs(LINKEDIN_SOURCES_DIR, exist_ok=True)
    file_path = os.path.join(LINKEDIN_SOURCES_DIR, safe_uploaded_filename(file.filename))
    with open(file_path, "wb") as f:
        f.write(file.file.read())
    ingest_linkedin_export(db, llm, file_path)
    return RedirectResponse(url="/profile", status_code=303)


@router.post("/sites/add")
def add_site(
    url: str = Form(...),
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_llm),
):
    ingest_site(db, llm, url.strip())
    return RedirectResponse(url="/profile", status_code=303)


@router.get("/github")
def github_candidates(request: Request):
    settings = get_settings()
    repos = []
    error = None
    try:
        repos = list_candidate_repos(settings.github_token)
    except Exception as exc:  # noqa: BLE001 - surfaced to the owner, not swallowed
        error = str(exc)
    return render(templates, request, "profile_github.html", {"repos": repos, "error": error})


@router.post("/github/ingest")
async def github_ingest(request: Request):
    """Kicks off the batch - actual ingestion happens one repo per request in
    github_ingest_step below, so the owner sees x/y progress instead of the
    page hanging for however long N sequential LLM calls take."""
    form = await request.form()
    selected = form.getlist("selected_repos")
    if not selected:
        return RedirectResponse(url="/profile", status_code=303)
    repos_param = ",".join(selected)
    return RedirectResponse(
        url=f"/profile/github/ingest/step?repos={repos_param}&done=0&total={len(selected)}",
        status_code=303,
    )


@router.get("/github/ingest/step")
def github_ingest_step(
    request: Request,
    repos: str = "",
    done: int = 0,
    total: int = 0,
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_llm),
):
    """Ingests exactly one repo, then renders a self-refreshing progress page
    pointing at the next one. Stateless by design - the remaining queue lives
    in the URL, not server-side, so a page reload or server restart mid-batch
    just re-does the current repo instead of losing track of the batch."""
    remaining = [r for r in repos.split(",") if r]
    if not remaining:
        return RedirectResponse(url="/profile", status_code=303)

    settings = get_settings()
    current = remaining[0]
    rest = remaining[1:]

    error = None
    try:
        ingest_repo(db, llm, settings.github_token, current)
    except Exception as exc:  # noqa: BLE001 - surfaced on the progress page, not swallowed
        error = str(exc)
    done += 1

    if rest:
        next_url = f"/profile/github/ingest/step?repos={','.join(rest)}&done={done}&total={total}"
    else:
        next_url = "/profile"

    return render(
        templates,
        request,
        "ingest_progress.html",
        {"done": done, "total": total, "current_repo": current, "error": error, "next_url": next_url},
    )


@router.post("/synthesize")
def synthesize(db: Session = Depends(get_db), llm: LLMProvider = Depends(get_llm)):
    synthesize_profile(db, llm)
    return RedirectResponse(url="/profile", status_code=303)


@router.post("/update")
def update_profile(
    summary: str = Form(""),
    target_roles: str = Form(""),
    skills: str = Form(""),
    db: Session = Depends(get_db),
):
    """Owner review/edit step before treating this as the master profile."""
    profile = db.query(Profile).first()
    if profile is None:
        profile = Profile()
        db.add(profile)

    profile.summary = summary.strip()
    profile.target_roles = [r.strip() for r in target_roles.splitlines() if r.strip()]

    parsed_skills = []
    for line in skills.splitlines():
        if "|" not in line:
            continue
        skill, _, evidence = line.partition("|")
        if skill.strip():
            parsed_skills.append({"skill": skill.strip(), "evidence_source": evidence.strip()})
    profile.skills = parsed_skills

    db.commit()
    return RedirectResponse(url="/profile", status_code=303)


@router.post("/cover-letter-template")
def update_cover_letter_template(cover_letter_template: str = Form(""), db: Session = Depends(get_db)):
    """Separate from /update so saving the template never clobbers the
    summary/roles/skills fields with the empty defaults of a form that doesn't
    include them."""
    profile = db.query(Profile).first()
    if profile is None:
        profile = Profile()
        db.add(profile)
    profile.cover_letter_template = cover_letter_template.strip()
    db.commit()
    return RedirectResponse(url="/profile", status_code=303)
