"""Company discovery UI: technopark directories -> candidates -> homepage
screening -> owner approves/rejects. Every step is owner-triggered; the
screening runs one candidate per request on a self-refreshing page (same
pattern as GitHub ingest), so it can be stopped at any point by navigating away."""
import os

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.companies.dedup import normalize_url
from app.db import get_db
from app.config import get_settings
from app.discovery.extra_sources import MAPPABLE_FIELDS, guess_mapping, table_rows
from app.discovery.liveness import check_liveness
from app.discovery.sector import candidate_sector, owner_sector_filter
from app.discovery.service import (
    approve,
    check_pending_liveness,
    ensure_default_sources,
    import_source,
    needs_site_check,
    reconcile_pending,
    reject,
)
from app.discovery.triage import triage_candidate
from app.llm.base import LLMProvider
from app.models import CandidateStatus, CompanyCandidate, DiscoverySource, OwnerSettings
from app.postings.filter import parse_terms
from app.web.deps import get_llm
from app.web.files import safe_uploaded_filename
from app.web.render import render

router = APIRouter(prefix="/discovery", tags=["discovery"])
templates = Jinja2Templates(directory="app/web/templates")

FIT_ORDER = {"high": 0, "medium": 1, "low": 2, "unknown": 3}


def _has_hard_flag(candidate: CompanyCandidate) -> bool:
    return any(f.get("type") == "hard" for f in (candidate.triage or {}).get("flags") or [])


def _unscreened_query(db: Session, source_id: int):
    query = db.query(CompanyCandidate).filter(
        CompanyCandidate.status == CandidateStatus.PENDING, CompanyCandidate.triaged_at.is_(None)
    )
    if source_id:
        query = query.filter(CompanyCandidate.source_id == source_id)
    return query


def _liveness_status(candidate: CompanyCandidate) -> str:
    return (candidate.liveness or {}).get("status", "")


def _screening_queue(db: Session, source_id: int, include_dead: bool) -> list[CompanyCandidate]:
    """Unscreened candidates; dead sites and off-sector companies are
    skipped (no LLM call for them) unless the owner forces them."""
    rules = owner_sector_filter(db)
    candidates = _unscreened_query(db, source_id).order_by(CompanyCandidate.id).all()
    return [
        c for c in candidates
        if include_dead or (_liveness_status(c) != "DEAD" and candidate_sector(rules, c)["result"] != "off")
    ]


@router.get("")
def discovery_page(
    request: Request,
    source_id: int = 0,
    fit: str = "",
    hide_hard: int = 0,
    status: str = "PENDING",
    imported: str = "",
    live: str = "",
    liveness_checked: str = "",
    sector: str = "fit",
    db: Session = Depends(get_db),
):
    ensure_default_sources(db)
    reconcile_pending(db)  # companies added since the import drop out of the queue
    sources = db.query(DiscoverySource).order_by(DiscoverySource.id).all()

    query = db.query(CompanyCandidate)
    if status in CandidateStatus.__members__:
        query = query.filter(CompanyCandidate.status == CandidateStatus[status])
    if source_id:
        query = query.filter(CompanyCandidate.source_id == source_id)
    rules = owner_sector_filter(db)
    candidates = query.all()
    sectors = {c.id: candidate_sector(rules, c) for c in candidates}
    pending = [c for c in db.query(CompanyCandidate).filter(CompanyCandidate.status == CandidateStatus.PENDING).all()]
    pending_sectors = [candidate_sector(rules, c)["result"] for c in pending]
    sector_counts = {key: pending_sectors.count(key) for key in ("match", "off", "unclear")}
    dead_count = sum(1 for c in pending if _liveness_status(c) == "DEAD")
    if sector == "fit":
        candidates = [c for c in candidates if sectors[c.id]["result"] != "off"]
    elif sector in ("match", "off", "unclear"):
        candidates = [c for c in candidates if sectors[c.id]["result"] == sector]
    if fit == "unscreened":
        candidates = [c for c in candidates if c.triaged_at is None]
    elif fit:
        candidates = [c for c in candidates if (c.triage or {}).get("fit") == fit]
    if hide_hard:
        candidates = [c for c in candidates if not _has_hard_flag(c)]
    if live == "unchecked":
        candidates = [c for c in candidates if not _liveness_status(c)]
    elif live:
        candidates = [c for c in candidates if _liveness_status(c) == live]
    # Best fits first; hard-flagged ones sink below their fit group; unscreened last.
    candidates.sort(
        key=lambda c: (
            c.triaged_at is None,
            FIT_ORDER.get((c.triage or {}).get("fit"), 3),
            _has_hard_flag(c),
            c.name.casefold(),
        )
    )

    stats = {
        s.id: {
            "pending": db.query(CompanyCandidate)
            .filter(CompanyCandidate.source_id == s.id, CompanyCandidate.status == CandidateStatus.PENDING)
            .count(),
            "unscreened": len(_screening_queue(db, s.id, False)),
            "dead": sum(1 for c in _unscreened_query(db, s.id).all() if _liveness_status(c) == "DEAD"),
        }
        for s in sources
    }
    return render(
        templates,
        request,
        "discovery.html",
        {
            "sources": sources,
            "stats": stats,
            "candidates": candidates,
            "filter_source_id": source_id,
            "filter_fit": fit,
            "filter_hide_hard": bool(hide_hard),
            "filter_status": status,
            "statuses": list(CandidateStatus),
            "imported": imported.split(",") if imported.count(",") == 3 else None,
            "has_hard_flag": _has_hard_flag,
            "filter_live": live,
            "liveness_checked": liveness_checked.split(",") if liveness_checked else None,
            "unchecked_liveness": sum(1 for c in pending if c.website_url and needs_site_check(c)),
            "sectors": sectors,
            "sector_rules": rules,
            "sector_counts": sector_counts,
            "dead_count": dead_count,
            "filter_sector": sector,
        },
    )


@router.post("/sources")
def add_source(
    name: str = Form(...),
    city: str = Form(""),
    url: str = Form(""),
    kind: str = Form("html"),
    items_path: str = Form(""),
    name_field: str = Form(""),
    website_field: str = Form(""),
    email_field: str = Form(""),
    sector_field: str = Form(""),
    location: str = Form(""),
    db: Session = Depends(get_db),
):
    """A directory page ("html"), a JSON endpoint copied from the browser's
    network tab ("json", with a field mapping), or GitHub organizations at a
    location ("github_orgs")."""
    kind = kind if kind in ("html", "json", "github_orgs") else "html"
    config = {}
    if kind == "json":
        config = {"items_path": items_path.strip(), "name": name_field.strip() or "name", "website": website_field.strip(),
                  "email": email_field.strip(), "sector": sector_field.strip()}
    if kind == "github_orgs":
        url = (location or city).strip()
    if url.strip():
        db.add(DiscoverySource(name=name.strip(), city=city.strip(), url=url.strip(), kind=kind, config=config))
        db.commit()
    return RedirectResponse(url="/discovery", status_code=303)


@router.post("/files")
async def upload_list(
    request: Request,
    file: UploadFile = File(...),
    name: str = Form(...),
    city: str = Form(""),
    db: Session = Depends(get_db),
):
    """Step 1 of a CSV/XLSX import: store the file, show its columns with a
    guessed mapping for the owner to confirm."""
    upload_dir = get_settings().discovery_uploads_dir
    os.makedirs(upload_dir, exist_ok=True)
    path = os.path.join(upload_dir, safe_uploaded_filename(file.filename))
    data = await file.read()
    with open(path, "wb") as f:
        f.write(data)
    rows = table_rows(path, data)
    headers = list(rows[0]) if rows else []
    source = DiscoverySource(name=name.strip(), city=city.strip(), url=f"file:{os.path.basename(path)}", kind="file",
                             config={"path": path, "mapping": guess_mapping(headers)})
    db.add(source)
    db.commit()
    return render(
        templates,
        request,
        "discovery_mapping.html",
        {"source": source, "headers": headers, "preview": rows[:5], "row_count": len(rows), "fields": MAPPABLE_FIELDS},
    )


@router.post("/files/{source_id}/mapping")
async def save_mapping(request: Request, source_id: int, db: Session = Depends(get_db), llm: LLMProvider = Depends(get_llm)):
    """Step 2: save the confirmed column mapping and import the rows."""
    source = db.get(DiscoverySource, source_id)
    if source is None or source.kind != "file":
        return RedirectResponse(url="/discovery", status_code=303)
    form = await request.form()
    mapping = {field: str(form.get(field) or "") for field in MAPPABLE_FIELDS if form.get(field)}
    source.config = {**(source.config or {}), "mapping": mapping}
    db.commit()
    return import_from_source(source_id, db, llm)


@router.post("/sources/{source_id}/import")
def import_from_source(
    source_id: int, db: Session = Depends(get_db), llm: LLMProvider = Depends(get_llm)
):
    source = db.get(DiscoverySource, source_id)
    if source is None:
        return RedirectResponse(url="/discovery", status_code=303)
    try:
        counts = import_source(db, llm, source)
    except Exception:  # noqa: BLE001 - recorded on the source row and shown there
        return RedirectResponse(url=f"/discovery?source_id={source_id}", status_code=303)
    summary = ",".join(str(counts[k]) for k in ("found", "new", "already_in_list", "known"))
    return RedirectResponse(url=f"/discovery?source_id={source_id}&imported={summary}", status_code=303)


@router.get("/triage/step")
def triage_step(
    request: Request,
    source_id: int = 0,
    done: int = 0,
    total: int = 0,
    include_dead: int = 0,
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_llm),
):
    """Screens exactly one candidate, then renders a page that refreshes into
    the next one. The queue lives in the DB (pending + not yet screened), so a
    reload or a stop/restart just continues where it left off. Sites the
    liveness check found DEAD are skipped unless `include_dead`."""
    queue = _screening_queue(db, source_id, bool(include_dead))
    if done == 0:
        total = len(queue)
    candidate = queue[0] if queue else None
    back_url = f"/discovery?source_id={source_id}" if source_id else "/discovery"
    if candidate is None:
        return RedirectResponse(url=back_url, status_code=303)

    error = None
    try:
        triage_candidate(db, llm, candidate)
        done += 1
    except Exception as exc:  # noqa: BLE001 - LLM down/out of credit: stop and show it
        db.rollback()
        error = str(exc)

    finished = done >= total or not _screening_queue(db, source_id, bool(include_dead))
    next_url = None if error or finished else f"/discovery/triage/step?source_id={source_id}&done={done}&total={total}&include_dead={include_dead}"
    return render(
        templates,
        request,
        "discovery_progress.html",
        {
            "done": done,
            "total": max(total, 1),
            "current": candidate.name,
            "error": error,
            "next_url": next_url or back_url,
            "auto_continue": next_url is not None or (finished and not error),
            "back_url": back_url,
        },
    )


def _safe_return(return_to: str) -> str:
    return return_to if return_to.startswith("/discovery") else "/discovery"


@router.get("/liveness/step")
def liveness_step(request: Request, checked: int = 0, total: int = 0, db: Session = Depends(get_db)):
    """"Check all pending": one batch of sites per request (no LLM), on the
    same self-refreshing progress page as screening."""
    summary = check_pending_liveness(db)
    checked += summary["checked"]
    total = total or checked + summary["remaining"]
    finished = summary["remaining"] == 0 or summary["checked"] == 0
    next_url = "/discovery?live=DEAD" if finished else f"/discovery/liveness/step?checked={checked}&total={total}"
    return render(
        templates,
        request,
        "discovery_progress.html",
        {
            "done": checked,
            "total": max(total, 1),
            "current": ", ".join(f"{k}: {v}" for k, v in summary["counts"].items()),
            "error": None,
            "next_url": next_url,
            "auto_continue": True,
            "back_url": "/discovery",
        },
    )


@router.post("/candidates/{candidate_id}/liveness")
def recheck_candidate_liveness(candidate_id: int, return_to: str = Form(""), db: Session = Depends(get_db)):
    candidate = db.get(CompanyCandidate, candidate_id)
    if candidate is not None and candidate.website_url:
        candidate.liveness = {**(candidate.liveness or {}), **check_liveness(candidate.website_url)}
        db.commit()
    return RedirectResponse(url=_safe_return(return_to), status_code=303)


@router.post("/candidates/{candidate_id}/website")
def set_candidate_website(
    candidate_id: int, website_url: str = Form(...), return_to: str = Form(""), db: Session = Depends(get_db)
):
    """The owner found the site themselves (e.g. via the Google search link) -
    the candidate goes back into the screening queue with it."""
    candidate = db.get(CompanyCandidate, candidate_id)
    url = normalize_url(website_url)
    if candidate is not None and candidate.status == CandidateStatus.PENDING and url:
        candidate.website_url = url
        candidate.triage = {}
        candidate.triaged_at = None
        db.commit()
    return RedirectResponse(url=_safe_return(return_to), status_code=303)


@router.post("/sector-filter")
def save_sector_filter(include: str = Form(""), exclude: str = Form(""), db: Session = Depends(get_db)):
    settings = db.query(OwnerSettings).first()
    if settings is None:
        settings = OwnerSettings()
        db.add(settings)
    settings.discovery_filter = {"include": parse_terms(include), "exclude": parse_terms(exclude)}
    db.commit()
    return RedirectResponse(url="/discovery", status_code=303)


@router.post("/reject-filtered")
def reject_filtered(which: str = Form(...), db: Session = Depends(get_db)):
    """Reject every pending candidate the free checks ruled out: "dead" sites
    or "off"-sector companies. The owner clicks it; nothing goes on its own."""
    rules = owner_sector_filter(db)
    pending = db.query(CompanyCandidate).filter(CompanyCandidate.status == CandidateStatus.PENDING).all()
    if which == "dead":
        ids = [c.id for c in pending if _liveness_status(c) == "DEAD"]
    elif which == "off":
        ids = [c.id for c in pending if candidate_sector(rules, c)["result"] == "off"]
    else:
        ids = []
    reject(db, ids)
    return RedirectResponse(url="/discovery", status_code=303)


@router.post("/decide")
async def decide(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    ids = [int(v) for v in form.getlist("candidate_ids") if str(v).isdigit()]
    action = form.get("action")
    changed = approve(db, ids) if action == "approve" else reject(db, ids) if action == "reject" else 0
    if "application/json" in request.headers.get("accept", ""):  # the row buttons (discovery.js)
        return JSONResponse({"action": action, "changed": changed})
    return RedirectResponse(url=_safe_return(str(form.get("return_to") or "")), status_code=303)
