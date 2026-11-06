"""Profile Builder web UI (CLAUDE.md section 3): ingest sources, view the
coverage ledger, run stage-2 synthesis, and review/edit the resulting Profile
before it's treated as the master profile.

CVs are NOT uploaded here - they live in the CVs tab (one CV entity serves both
purposes: attached to outgoing applications AND ingested here as a profile
source), so there's only ever one place to manage CV files."""
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.applications.evidence import candidate_items
from app.config import get_settings
from app.db import get_db
from app.llm.base import LLMProvider
from app.applications.facts import approve_fact
from app.models import CV, Profile, ProfileFact, ProfileFactStatus, ProfileItem, ProfileItemStatus
from app.profile_builder.coverage import ledger_summary
from app.profile_builder.cv_parser import ingest_cv
from app.profile_builder.github_ingest import ingest_repo, list_candidate_repos
from app.profile_builder.linkedin_ingest import ingest_linkedin_export
from app.profile_builder.reingest import reingest_item
from app.profile_builder.repo_changes import check_for_changes, reanalyze_repo
from app.profile_builder.records import (
    accept_pending,
    apply_owner_edit,
    dismiss_pending,
    record_fields,
    to_form_value,
)
from app.profile_builder.site_ingest import ingest_site
from app.profile_builder.synthesis import synthesize_profile
from app.profile_builder.writeup import generate_writeup, unknown_tags
from app.web.deps import get_draft_llm, get_llm
from app.web.errors import NotFoundError
from app.web.files import safe_uploaded_filename
from app.web.render import render

router = APIRouter(prefix="/profile", tags=["profile"])
templates = Jinja2Templates(directory="app/web/templates")

LINKEDIN_SOURCES_DIR = "data/profile_sources/linkedin"


def _ingested_cv_paths(db: Session) -> set[str]:
    return {
        item.parent_ref or item.source_ref
        for item in db.query(ProfileItem).filter(ProfileItem.source_type == "cv").all()
    }


@router.get("")
def profile_home(request: Request, reanalyzed: int = 0, db: Session = Depends(get_db)):
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
            "reanalyzed": bool(reanalyzed),
            "pending_count": sum(1 for item in items if item.pending_update),
            "proposed_facts": db.query(ProfileFact).filter(ProfileFact.status == ProfileFactStatus.PROPOSED).order_by(ProfileFact.id).all(),
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
def retry_profile_item(
    item_id: int,
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_llm),
    draft_llm: LLMProvider = Depends(get_draft_llm),
):
    """Re-runs extraction for one item using its existing source - for items
    that came back needs_review because the LLM call itself failed (rate
    limit, no credit, etc.), not because the source was genuinely thin. The
    record is updated in place, so owner edits and answers survive."""
    item = db.get(ProfileItem, item_id)
    if item is None:
        return RedirectResponse(url="/profile", status_code=303)
    reingest_item(db, llm, draft_llm, item)
    return RedirectResponse(url="/profile", status_code=303)


@router.get("/items/{item_id}")
def profile_item_detail(request: Request, item_id: int, saved: int = 0, db: Session = Depends(get_db)):
    item = db.get(ProfileItem, item_id)
    if item is None:
        raise NotFoundError(f"Profile item {item_id} not found")
    extracted = item.extracted or {}
    fields = [
        {"name": name, "kind": kind, "value": to_form_value(extracted.get(name), kind),
         "owner_edited": name in (item.owner_edited_fields or [])}
        for name, kind in record_fields(extracted)
    ]
    pending = [
        {"name": name, "old": to_form_value(extracted.get(name), kind), "new": to_form_value(item.pending_update[name], kind)}
        for name, kind in record_fields(extracted)
        if name in (item.pending_update or {})
    ]
    answered = {fact.get("question") for fact in item.owner_facts or []}
    questions = [q for q in extracted.get("open_questions") or [] if q not in answered]
    return render(
        templates,
        request,
        "profile_item.html",
        {"item": item, "fields": fields, "pending": pending, "questions": questions, "saved": bool(saved)},
    )


@router.post("/items/{item_id}/edit")
async def edit_profile_item(request: Request, item_id: int, db: Session = Depends(get_db)):
    item = db.get(ProfileItem, item_id)
    if item is None:
        raise NotFoundError(f"Profile item {item_id} not found")
    form = await request.form()
    changed = apply_owner_edit(item, {key: str(value) for key, value in form.items()})
    if changed and item.status == ProfileItemStatus.NEEDS_REVIEW:
        item.status = ProfileItemStatus.ANALYZED  # the owner vouched for it
    db.commit()
    return RedirectResponse(url=f"/profile/items/{item_id}?saved=1", status_code=303)


@router.post("/items/{item_id}/pending/{field}")
def resolve_pending_field(item_id: int, field: str, action: str = Form(...), db: Session = Depends(get_db)):
    item = db.get(ProfileItem, item_id)
    if item is None:
        raise NotFoundError(f"Profile item {item_id} not found")
    if action == "accept":
        accept_pending(item, field)
    else:
        dismiss_pending(item, field)
    db.commit()
    return RedirectResponse(url=f"/profile/items/{item_id}", status_code=303)


@router.post("/items/{item_id}/answers")
async def answer_item_questions(request: Request, item_id: int, db: Session = Depends(get_db)):
    """Owner interview (roadmap phase 1): answers to the record's open
    questions become owner-stated facts, which count as evidence."""
    item = db.get(ProfileItem, item_id)
    if item is None:
        raise NotFoundError(f"Profile item {item_id} not found")
    form = await request.form()
    questions = (item.extracted or {}).get("open_questions") or []
    facts = list(item.owner_facts or [])
    now = datetime.now(timezone.utc).isoformat()
    for index, question in enumerate(questions):
        answer = str(form.get(f"answer_{index}", "")).strip()
        if answer:
            facts = [f for f in facts if f.get("question") != question]
            facts.append({"question": question, "answer": answer, "source": "owner", "at": now})
    extra = str(form.get("extra_fact", "")).strip()
    if extra:
        facts.append({"question": "", "answer": extra, "source": "owner", "at": now})
    item.owner_facts = facts
    db.commit()
    return RedirectResponse(url=f"/profile/items/{item_id}?saved=1", status_code=303)


@router.post("/items/{item_id}/facts/{index}/delete")
def delete_item_fact(item_id: int, index: int, db: Session = Depends(get_db)):
    item = db.get(ProfileItem, item_id)
    if item is None:
        raise NotFoundError(f"Profile item {item_id} not found")
    facts = list(item.owner_facts or [])
    if 0 <= index < len(facts):
        facts.pop(index)
        item.owner_facts = facts
        db.commit()
    return RedirectResponse(url=f"/profile/items/{item_id}", status_code=303)


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
    follow_subpages: str = Form(""),
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_llm),
):
    ingest_site(db, llm, url.strip(), follow_subpages=bool(follow_subpages))
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
    draft_llm: LLMProvider = Depends(get_draft_llm),
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
        ingest_repo(db, llm, settings.github_token, current, draft_llm=draft_llm)
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


@router.get("/github/changes")
def github_changes(request: Request, db: Session = Depends(get_db)):
    """Owner-triggered change check - GitHub API only, no LLM call."""
    report, error = None, None
    try:
        report = check_for_changes(db, get_settings().github_token)
    except Exception as exc:  # noqa: BLE001 - surfaced to the owner
        error = str(exc)
    return render(templates, request, "profile_changes.html", {"report": report, "error": error})


@router.post("/items/{item_id}/reanalyze")
def reanalyze_item(
    item_id: int,
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_llm),
    draft_llm: LLMProvider = Depends(get_draft_llm),
):
    item = db.get(ProfileItem, item_id)
    if item is None or item.source_type != "github_repo":
        raise NotFoundError(f"Repo record {item_id} not found")
    reanalyze_repo(db, llm, draft_llm, get_settings().github_token, item)
    return RedirectResponse(url=f"/profile/items/{item_id}", status_code=303)


@router.get("/github/reanalyze/step")
def github_reanalyze_step(
    request: Request,
    ids: str = "",
    done: int = 0,
    total: int = 0,
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_llm),
    draft_llm: LLMProvider = Depends(get_draft_llm),
):
    """"Re-analyze all changed", one repo per request (same stateless
    progress page as the first ingest)."""
    remaining = [int(i) for i in ids.split(",") if i.strip().isdigit()]
    if not remaining:
        return RedirectResponse(url="/profile?reanalyzed=1", status_code=303)
    item = db.get(ProfileItem, remaining[0])
    error = None
    current = item.source_ref if item else str(remaining[0])
    if item is not None:
        try:
            reanalyze_repo(db, llm, draft_llm, get_settings().github_token, item)
        except Exception as exc:  # noqa: BLE001 - shown on the progress page
            error = str(exc)
    done += 1
    rest = remaining[1:]
    next_url = (
        f"/profile/github/reanalyze/step?ids={','.join(map(str, rest))}&done={done}&total={total}"
        if rest
        else "/profile?reanalyzed=1"
    )
    return render(
        templates,
        request,
        "ingest_progress.html",
        {"done": done, "total": total, "current_repo": current, "error": error, "next_url": next_url},
    )


@router.post("/items/{item_id}/skip")
def skip_profile_item(item_id: int, db: Session = Depends(get_db)):
    """A repo that no longer exists: kept for the ledger, never used as evidence."""
    item = db.get(ProfileItem, item_id)
    if item is not None:
        item.status = ProfileItemStatus.SKIPPED
        db.commit()
    return RedirectResponse(url="/profile/github/changes", status_code=303)


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


@router.post("/facts/{fact_id}")
def decide_fact(fact_id: int, action: str = Form(...), text: str = Form(""), db: Session = Depends(get_db)):
    """A fact the owner stated in feedback or an edit: approve (it becomes an
    evidence record, editable first) or reject."""
    fact = db.get(ProfileFact, fact_id)
    if fact is not None and fact.status == ProfileFactStatus.PROPOSED:
        if action == "approve":
            approve_fact(db, fact, text or None)
        else:
            fact.status = ProfileFactStatus.REJECTED
            db.commit()
    return RedirectResponse(url="/profile#facts", status_code=303)


@router.post("/writeup/generate")
def writeup_generate(db: Session = Depends(get_db), draft_llm: LLMProvider = Depends(get_draft_llm)):
    generate_writeup(db, draft_llm)
    return RedirectResponse(url="/profile#writeup", status_code=303)


@router.post("/writeup")
def writeup_save(writeup: str = Form(""), db: Session = Depends(get_db)):
    profile = db.query(Profile).first()
    if profile is None:
        profile = Profile()
        db.add(profile)
    profile.writeup = writeup.strip()
    profile.writeup_unknown_tags = unknown_tags(profile.writeup, {item.id for item in candidate_items(db)})
    db.commit()
    return RedirectResponse(url="/profile#writeup", status_code=303)


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
