"""Company discovery UI: technopark directories -> candidates -> homepage
screening -> owner approves/rejects. Every step is owner-triggered; the
screening runs one candidate per request on a self-refreshing page (same
pattern as GitHub ingest), so it can be stopped at any point by navigating away."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.companies.dedup import normalize_url
from app.db import get_db
from app.discovery.service import approve, ensure_default_sources, import_source, reconcile_pending, reject
from app.discovery.triage import triage_candidate
from app.llm.base import LLMProvider
from app.models import CandidateStatus, CompanyCandidate, DiscoverySource
from app.web.deps import get_llm
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


@router.get("")
def discovery_page(
    request: Request,
    source_id: int = 0,
    fit: str = "",
    hide_hard: int = 0,
    status: str = "PENDING",
    imported: str = "",
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
    candidates = query.all()
    if fit == "unscreened":
        candidates = [c for c in candidates if c.triaged_at is None]
    elif fit:
        candidates = [c for c in candidates if (c.triage or {}).get("fit") == fit]
    if hide_hard:
        candidates = [c for c in candidates if not _has_hard_flag(c)]
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
            "unscreened": _unscreened_query(db, s.id).count(),
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
        },
    )


@router.post("/sources")
def add_source(
    name: str = Form(...), city: str = Form(""), url: str = Form(...), db: Session = Depends(get_db)
):
    db.add(DiscoverySource(name=name.strip(), city=city.strip(), url=url.strip(), kind="html"))
    db.commit()
    return RedirectResponse(url="/discovery", status_code=303)


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
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_llm),
):
    """Screens exactly one candidate, then renders a page that refreshes into
    the next one. The queue lives in the DB (pending + not yet screened), so a
    reload or a stop/restart just continues where it left off."""
    queue = _unscreened_query(db, source_id)
    if done == 0:
        total = queue.count()
    candidate = queue.order_by(CompanyCandidate.id).first()
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

    finished = done >= total or queue.count() == 0
    next_url = None if error or finished else f"/discovery/triage/step?source_id={source_id}&done={done}&total={total}"
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


@router.post("/decide")
async def decide(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    ids = [int(v) for v in form.getlist("candidate_ids") if str(v).isdigit()]
    action = form.get("action")
    changed = approve(db, ids) if action == "approve" else reject(db, ids) if action == "reject" else 0
    if "application/json" in request.headers.get("accept", ""):  # the row buttons (discovery.js)
        return JSONResponse({"action": action, "changed": changed})
    return RedirectResponse(url=_safe_return(str(form.get("return_to") or "")), status_code=303)
