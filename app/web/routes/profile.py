"""Profile Builder web UI (CLAUDE.md section 3): ingest sources, view the
coverage ledger, run stage-2 synthesis, and review/edit the resulting Profile
before it's treated as the master profile.

CVs are NOT uploaded here - they live in the CVs tab (one CV entity serves both
purposes: attached to outgoing applications AND ingested here as a profile
source), so there's only ever one place to manage CV files."""
import os
import re
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
from app.models import CV, CVProject, OwnerSettings, Profile, ProfileFact, ProfileFactStatus, ProfileItem, ProfileItemStatus
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


GROUPS = (
    ("projects", ("github_repo", "site")),
    ("experience", ("cv", "linkedin")),
    ("facts", ("owner",)),
)


def item_title(item: ProfileItem) -> str:
    """A readable name for a record (never the raw file#entry ref)."""
    extracted = item.extracted or {}
    if extracted.get("entry_type"):
        dates = " – ".join(p for p in (extracted.get("start"), extracted.get("end")) if p)
        return " · ".join(p for p in (extracted.get("role") or extracted.get("title", ""), dates) if p)
    if item.source_type == "github_repo":
        return item.source_ref.split("/")[-1]
    if item.source_type == "owner":
        return extracted.get("purpose", "")[:80]
    return item.source_ref.removeprefix("https://").removeprefix("http://")


def open_questions(item: ProfileItem) -> list[str]:
    """Questions the record couldn't answer, minus answered and skipped ones."""
    done = {f.get("question") for f in item.owner_facts or []} | set((item.extracted or {}).get("skipped_questions") or [])
    return [q for q in (item.extracted or {}).get("open_questions") or [] if q not in done]


def _next_or(next_url: str, fallback: str) -> str:
    return next_url if next_url.startswith("/profile") and "//" not in next_url else fallback


def _aware(moment):
    from datetime import timezone as tz

    return moment if moment is None or moment.tzinfo else moment.replace(tzinfo=tz.utc)


def records_changed_since_update(profile: Profile | None, items: list[ProfileItem]) -> int:
    """Analyzed records added or re-analyzed after the profile was last built."""
    analyzed = [i for i in items if i.status == ProfileItemStatus.ANALYZED]
    built_at = _aware(profile.updated_at) if profile and profile.summary else None
    if built_at is None:
        return len(analyzed)
    return sum(
        1 for i in analyzed
        if (_aware(i.created_at) and _aware(i.created_at) > built_at)
        or ((i.source_version or {}).get("analyzed_at", "") > built_at.isoformat())
    )


def needs_attention(items: list[ProfileItem], pending_cvs: list[CV]) -> list[dict]:
    """Things that block a good profile: unread CVs, failed records, re-read
    changes waiting for review."""
    steps = []
    if pending_cvs:
        steps.append({"key": "ingest_cvs", "count": len(pending_cvs)})
    failed = [i for i in items if i.status == ProfileItemStatus.NEEDS_REVIEW]
    if failed:
        steps.append({"key": "failed", "count": len(failed), "href": f"/profile/items/{failed[0].id}"})
    pending = [i for i in items if i.pending_update]
    if pending:
        steps.append({"key": "pending", "count": len(pending), "href": f"/profile/items/{pending[0].id}"})
    return steps


@router.get("")
def profile_home(
    request: Request,
    reanalyzed: int = 0,
    ingested: int = -1,
    skipped: int = 0,
    updated: int = 0,
    failed: str = "",
    db: Session = Depends(get_db),
):
    profile = db.query(Profile).first()
    items = db.query(ProfileItem).order_by(ProfileItem.created_at.desc()).all()
    ingested_paths = _ingested_cv_paths(db)
    cvs = db.query(CV).filter(CV.file_path != "").all()
    pending_cvs = [cv for cv in cvs if cv.file_path not in ingested_paths]
    by_ref = {i.source_ref: i for i in items}
    return render(
        templates,
        request,
        "profile.html",
        {
            "profile": profile,
            "groups": [(key, [i for i in items if i.source_type in types]) for key, types in GROUPS],
            "items_count": len(items),
            "ledger": ledger_summary(db),
            "pending_cvs": pending_cvs,
            "has_any_cv": bool(cvs),
            "steps": needs_attention(items, pending_cvs),
            "changed": records_changed_since_update(profile, items),
            "questions": [
                (item, [(index, q) for index, q in enumerate((item.extracted or {}).get("open_questions") or []) if q in open_questions(item)])
                for item in items
                if item.status == ProfileItemStatus.ANALYZED and open_questions(item)
            ],
            "error": request.query_params.get("error", ""),
            "item_title": item_title,
            "open_questions": open_questions,
            "ingest_summary": {"done": ingested, "skipped": skipped, "updated": updated,
                               "failed": [(name, by_ref.get(name)) for name in failed.split(",") if name]} if ingested >= 0 else None,
            "proposed_facts": db.query(ProfileFact).filter(ProfileFact.status == ProfileFactStatus.PROPOSED).order_by(ProfileFact.id).all(),
        },
    )


@router.post("/items/{item_id}/delete")
def delete_profile_item(item_id: int, db: Session = Depends(get_db)):
    """A deleted repo is also excluded, so the GitHub list never offers it again
    (it can be restored there)."""
    item = db.get(ProfileItem, item_id)
    if item is not None:
        if item.source_type == "github_repo":
            settings = _owner_settings(db)
            settings.excluded_repos = sorted(set(settings.excluded_repos or []) | {item.source_ref})
        for project in db.query(CVProject).filter(CVProject.profile_item_id == item.id).all():
            for bullet in project.bullets:
                db.delete(bullet)
            db.delete(project)
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
    skipped = list((item.extracted or {}).get("skipped_questions") or [])
    for index, question in enumerate(questions):
        if form.get(f"skip_{index}"):
            skipped.append(question)  # not applicable - stop asking, claim nothing
            continue
        answer = str(form.get(f"answer_{index}", "")).strip()
        if answer:
            facts = [f for f in facts if f.get("question") != question]
            facts.append({"question": question, "answer": answer, "source": "owner", "at": now})
    extra = str(form.get("extra_fact", "")).strip()
    if extra:
        facts.append({"question": "", "answer": extra, "source": "owner", "at": now})
    item.owner_facts = facts
    if skipped != list((item.extracted or {}).get("skipped_questions") or []):
        item.extracted = {**(item.extracted or {}), "skipped_questions": sorted(set(skipped))}
    db.commit()
    return RedirectResponse(url=_next_or(str(form.get("next", "")), f"/profile/items/{item_id}?saved=1"), status_code=303)


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


def _owner_settings(db: Session) -> OwnerSettings:
    settings = db.query(OwnerSettings).first()
    if settings is None:
        settings = OwnerSettings()
        db.add(settings)
        db.commit()
    return settings


def current_pushed_at(token: str, full_name: str) -> str:
    """One GitHub call: when the repo was last pushed (same format as source_version)."""
    from github import Github

    pushed = Github(token).get_repo(full_name).pushed_at
    return pushed.isoformat() if pushed else ""


def repo_status(repo: dict, item: ProfileItem | None, excluded: set[str]) -> str:
    """new | unchanged | changed | failed | excluded - decides the default
    checkbox and what ingesting it does (unchanged costs nothing)."""
    if repo["full_name"] in excluded:
        return "excluded"
    if item is None:
        return "new"
    if item.status != ProfileItemStatus.ANALYZED:
        return "failed"
    version = item.source_version or {}
    if version.get("pushed_at") and version["pushed_at"] == repo.get("pushed_at"):
        return "unchanged"
    return "changed"


@router.get("/github")
def github_candidates(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    repos = []
    error = None
    try:
        repos = list_candidate_repos(settings.github_token)
    except Exception as exc:  # noqa: BLE001 - surfaced to the owner, not swallowed
        error = str(exc)
    items = {i.source_ref: i for i in db.query(ProfileItem).filter(ProfileItem.source_type == "github_repo").all()}
    excluded = set(_owner_settings(db).excluded_repos or [])
    for repo in repos:
        repo["status"] = repo_status(repo, items.get(repo["full_name"]), excluded)
        repo["checked"] = repo["status"] in ("changed", "failed") or (repo["status"] == "new" and repo["suggested_include"])
        repo["item_id"] = items[repo["full_name"]].id if repo["full_name"] in items else None
    order = {"changed": 0, "failed": 1, "new": 2, "unchanged": 3, "excluded": 4}
    repos.sort(key=lambda r: (order[r["status"]], not r["suggested_include"], r["full_name"].casefold()))
    counts = {status: sum(1 for r in repos if r["status"] == status) for status in order}
    return render(templates, request, "profile_github.html", {"repos": repos, "error": error, "counts": counts})


@router.post("/github/restore")
def github_restore(full_name: str = Form(...), db: Session = Depends(get_db)):
    settings = _owner_settings(db)
    settings.excluded_repos = [r for r in settings.excluded_repos or [] if r != full_name]
    db.commit()
    return RedirectResponse(url="/profile/github", status_code=303)


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
    failed: str = "",
    skipped: int = 0,
    updated: int = 0,
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_llm),
    draft_llm: LLMProvider = Depends(get_draft_llm),
):
    """One repo per request on a self-refreshing page (the queue lives in the
    URL, so a reload just redoes the current repo):
    - analyzed and unchanged since (same pushed_at): skipped, no AI call;
    - analyzed but changed: re-analysis, proposed as a diff (never overwrites);
    - new or failed before: full analysis.
    Failures are collected and summarized at the end, not flashed by."""
    remaining = [r for r in repos.split(",") if r]
    failures = [f for f in failed.split(",") if f]
    if not remaining:
        return RedirectResponse(url=f"/profile?ingested={done}&skipped={skipped}&updated={updated}&failed={','.join(failures)}", status_code=303)

    settings = get_settings()
    current, rest = remaining[0], remaining[1:]
    item = db.query(ProfileItem).filter(ProfileItem.source_type == "github_repo", ProfileItem.source_ref == current).first()
    outcome = "analyzed"
    try:
        pushed_at = current_pushed_at(settings.github_token, current) if item else ""
        if item is not None and item.status == ProfileItemStatus.ANALYZED and (item.source_version or {}).get("pushed_at") == pushed_at and pushed_at:
            outcome = "skipped"
        elif item is not None and item.status == ProfileItemStatus.ANALYZED:
            reanalyze_repo(db, llm, draft_llm, settings.github_token, item)
            outcome = "updated"
        else:
            result = ingest_repo(db, llm, settings.github_token, current, draft_llm=draft_llm)
            if result.status != ProfileItemStatus.ANALYZED:
                outcome = "failed"
    except Exception:  # noqa: BLE001 - collected and shown at the end
        outcome = "failed"
    done += 1
    skipped += outcome == "skipped"
    updated += outcome == "updated"
    if outcome == "failed":
        failures.append(current)
    query = f"done={done}&total={total}&failed={','.join(failures)}&skipped={skipped}&updated={updated}"
    next_url = f"/profile/github/ingest/step?repos={','.join(rest)}&{query}" if rest else f"/profile?ingested={done}&skipped={skipped}&updated={updated}&failed={','.join(failures)}"
    return render(
        templates,
        request,
        "ingest_progress.html",
        {"done": done, "total": total, "current_repo": current, "outcome": outcome, "failures": failures,
         "skipped": skipped, "updated": updated, "next_url": next_url},
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
    next: str = Form(""),
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_llm),
    draft_llm: LLMProvider = Depends(get_draft_llm),
):
    item = db.get(ProfileItem, item_id)
    if item is None or item.source_type != "github_repo":
        raise NotFoundError(f"Repo record {item_id} not found")
    reanalyze_repo(db, llm, draft_llm, get_settings().github_token, item)
    return RedirectResponse(url=_next_or(next, f"/profile/items/{item_id}"), status_code=303)


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


@router.post("/github/add-one")
def github_add_one(repo: str = Form(...), db: Session = Depends(get_db)):
    """Read just this repo into the profile (a new one, or one that changed):
    nothing else is re-read. Accepts owner/name or a github.com URL."""
    name = repo.strip().removeprefix("https://").removeprefix("http://").removeprefix("www.").removeprefix("github.com/")
    name = "/".join(name.strip("/").split("/")[:2]).removesuffix(".git")
    if name.count("/") != 1:
        return RedirectResponse(url="/profile?error=repo#sources", status_code=303)
    settings = _owner_settings(db)
    if name in (settings.excluded_repos or []):  # asking for it by name restores it
        settings.excluded_repos = [r for r in settings.excluded_repos if r != name]
        db.commit()
    return RedirectResponse(url=f"/profile/github/ingest/step?repos={name}&done=0&total=1", status_code=303)


@router.post("/update-all")
def update_all(
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_llm),
    draft_llm: LLMProvider = Depends(get_draft_llm),
):
    """The one "update my profile" button: the short profile (summary, roles,
    skills - what letters and fit checks read) and the long "about me"
    document, both rebuilt from every analyzed record."""
    synthesize_profile(db, llm)
    generate_writeup(db, draft_llm)
    return RedirectResponse(url="/profile#about", status_code=303)


@router.post("/about")
def save_about(
    summary: str = Form(""),
    target_roles: str = Form(""),
    writeup: str = Form(""),
    db: Session = Depends(get_db),
):
    """One edit for the whole "about me": the short summary, target roles and
    the long document. Skills are kept as they are."""
    profile = db.query(Profile).first()
    if profile is None:
        profile = Profile(skills=[])
        db.add(profile)
    profile.summary = summary.strip()
    profile.target_roles = [r.strip() for r in re.split(r"[,\n]+", target_roles) if r.strip()]
    profile.writeup = writeup.strip()
    profile.writeup_unknown_tags = unknown_tags(profile.writeup, {item.id for item in candidate_items(db)})
    db.commit()
    return RedirectResponse(url="/profile#about", status_code=303)


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
def update_cover_letter_template(cover_letter_template: str = Form(""), next: str = Form(""), db: Session = Depends(get_db)):
    """Separate from /update so saving the template never clobbers the
    summary/roles/skills fields with the empty defaults of a form that doesn't
    include them."""
    profile = db.query(Profile).first()
    if profile is None:
        profile = Profile()
        db.add(profile)
    profile.cover_letter_template = cover_letter_template.strip()
    db.commit()
    return RedirectResponse(url="/preferences#letter-template" if next == "/preferences" else "/profile", status_code=303)
