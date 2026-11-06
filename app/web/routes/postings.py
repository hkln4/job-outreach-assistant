"""Job postings UI (roadmap phase 3): add a posting (URL or pasted text), see
its gates, fit and legitimacy flags, match its company, draft an application
for it; read public ATS boards; check careers pages for new postings. Every
action is a click - nothing runs in the background."""
import json
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.applications.drafting import create_draft_application
from app.companies.linkedin_fetch import linkedin_job_id
from app.config import get_settings
from app.db import get_db
from app.llm.base import LLMProvider
from app.models import AtsBoard, Company, JobPosting, OwnerSettings, PostingStatus, Preference
from app.postings.ats import SUPPORTED_PROVIDERS, boards_from_research, import_board_postings, read_board
from app.postings.careers_watch import watch_all
from app.postings.evaluation import evaluate_fit
from app.postings.filter import LEVELS, check_posting, owner_filter, parse_terms
from app.postings.location import MODES
from app.postings.search import SearchNotConfigured, discover_boards
from app.postings.service import (
    add_posting,
    approve_proposed_company,
    check_expiry,
    has_unconfirmed_hard_flag,
    parse_and_evaluate,
    set_posting_status,
)
from app.web.deps import get_draft_llm, get_llm, get_review_llm
from app.web.errors import NotFoundError
from app.web.render import render

router = APIRouter(prefix="/postings", tags=["postings"])
templates = Jinja2Templates(directory="app/web/templates")

VERDICT_ORDER = {"strong": 0, "possible": 1, "unknown": 2, "weak": 3, "blocked": 4}


def _posting_or_404(db: Session, posting_id: int) -> JobPosting:
    posting = db.get(JobPosting, posting_id)
    if posting is None:
        raise NotFoundError(f"Posting {posting_id} not found")
    return posting


SEGMENTS = {
    "new": "Found automatically, waiting for you to keep or dismiss",
    "kept": "Kept or pasted by you, still open",
    "applied": "Applied",
    "closed": "Dismissed or expired",
    "sources": "Where postings come from",
}


def _segment_query(db: Session, seg: str):
    query = db.query(JobPosting)
    if seg == "new":
        return query.filter(JobPosting.inbox.is_(True), JobPosting.status == PostingStatus.NEW)
    if seg == "kept":
        return query.filter(JobPosting.inbox.is_(False), JobPosting.status.in_([PostingStatus.NEW, PostingStatus.SHORTLISTED]))
    if seg == "applied":
        return query.filter(JobPosting.status == PostingStatus.APPLIED)
    return query.filter(JobPosting.status.in_([PostingStatus.DISMISSED, PostingStatus.EXPIRED]))


def _detail_context(db: Session, posting: JobPosting, error: str = "") -> dict:
    copies = [db.get(JobPosting, c["id"]) for c in (posting.extra or {}).get("copies", [])]
    return {
        "posting": posting,
        "preferences": {p.id: p for p in db.query(Preference).all()},
        "hard_blocked": has_unconfirmed_hard_flag(posting),
        "statuses": list(PostingStatus),
        "copies": [c for c in copies if c is not None],
        "filter_check": check_posting(owner_filter(db), posting),
        "error": error,
    }


def _back(seg: str = "", open_id: int | None = None, **extra: str) -> RedirectResponse:
    params = {"seg": seg} if seg else {}
    if open_id:
        params["open"] = str(open_id)
    params.update({k: v for k, v in extra.items() if v})
    return RedirectResponse(url="/postings" + ("?" + urlencode(params) if params else ""), status_code=303)


@router.get("")
def postings_index(
    request: Request,
    seg: str = "",
    open: int | None = None,
    show_all: int = 0,
    error: str = "",
    report: str = "",
    db: Session = Depends(get_db),
):
    """One page: the filter on top, the list on the left, the selected posting
    on the right, the sources in their own tab. Postings outside the filter
    are hidden (never deleted) unless the owner asks to see them."""
    rules = owner_filter(db)
    counts = {}
    for key in ("new", "kept"):
        counts[key] = sum(1 for p in _segment_query(db, key).all() if check_posting(rules, p)["match"])
    if seg not in SEGMENTS:
        seg = "new" if counts["new"] else "kept"
    rows, hidden = [], 0
    if seg != "sources":
        everything = sorted(
            _segment_query(db, seg).all(),
            key=lambda p: (VERDICT_ORDER.get((p.fit or {}).get("verdict", "unknown"), 2), -(p.id or 0)),
        )
        for posting in everything:
            verdict = check_posting(rules, posting)
            if verdict["match"] or show_all:
                rows.append((posting, verdict))
            else:
                hidden += 1
    selected = db.get(JobPosting, open) if open else None
    settings = get_settings()
    context = {
        "seg": seg,
        "segments": list(SEGMENTS),
        "counts": counts,
        "rows": rows,
        "hidden": hidden,
        "show_all": bool(show_all),
        "rules": rules,
        "levels": LEVELS,
        "modes": MODES,
        "error": error,
        "report": _decode_report(report),
        "boards": db.query(AtsBoard).order_by(AtsBoard.company_name).all() if seg == "sources" else [],
        "providers": SUPPORTED_PROVIDERS,
        "search_enabled": settings.search_api_enabled and bool(settings.search_api_key),
        "linkedin_enabled": settings.linkedin_research_enabled,
        "careers_companies": db.query(Company).filter(Company.careers_url.isnot(None), Company.careers_url != "").count(),
    }
    if selected is not None:
        context.update(_detail_context(db, selected))
        context["open_id"] = selected.id
    return render(templates, request, "postings.html", context)


def _encode_report(lines: list[dict]) -> str:
    return json.dumps(lines, separators=(",", ":"))


def _decode_report(raw: str) -> list[dict]:
    try:
        data = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [d for d in data if isinstance(d, dict)][:50]


@router.post("/filter")
async def save_filter(request: Request, db: Session = Depends(get_db)):
    """The whole filter in one form: roles, excluded words, levels, the
    owner's places (each with its work modes; rows are added by filling the
    empty one and removed with its button), the rest of Turkey, abroad."""
    form = await request.form()
    remove = str(form.get("remove_place", ""))
    places = []
    for index in range(int(form.get("place_count", 0) or 0) + 1):
        place = str(form.get(f"place_{index}", "")).strip()
        if place and str(index) != remove:
            places.append({"place": place, "modes": [m for m in form.getlist(f"modes_{index}") if m in MODES]})
    settings = db.query(OwnerSettings).first()
    if settings is None:
        settings = OwnerSettings()
        db.add(settings)
    settings.posting_filter = {
        "roles": parse_terms(str(form.get("roles", ""))),
        "exclude": parse_terms(str(form.get("exclude", ""))),
        "levels": [lv for lv in form.getlist("levels") if lv in LEVELS],
        "places": places,
        "turkey_modes": [m for m in form.getlist("turkey_modes") if m in MODES],
        "abroad_modes": [m for m in form.getlist("abroad_modes") if m in MODES],
        "open_terms": parse_terms(str(form.get("open_terms", ""))),
        "bare_remote": "show" if form.get("bare_remote") == "show" else "skip",
    }
    db.commit()
    return _back(str(form.get("seg", "")))


@router.post("")
def create_posting(
    url: str = Form(""),
    text: str = Form(""),
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_llm),
):
    """A posting the owner pasted: straight to "kept" (he chose it), opened."""
    try:
        posting, _ = add_posting(db, llm, url=url.strip(), text=text, source="linkedin" if linkedin_job_id(url) else "manual")
    except ValueError as exc:
        return _back("sources", error=str(exc)[:300])
    return _back("kept", posting.id)


@router.post("/check")
def check_everything(db: Session = Depends(get_db), llm: LLMProvider = Depends(get_llm)):
    """The one "find new postings" button: every ATS board (no LLM), then the
    careers pages (the LLM only reads a page whose text changed)."""
    lines = [_read_one_board(db, board) for board in db.query(AtsBoard).all()]
    summary = watch_all(db, llm)
    lines.append({"source": "careers pages", "read": summary["checked"], "imported": summary["new_postings"], "changed": summary["changed"]})
    return _back("new", report=_encode_report(lines))


def _read_one_board(db: Session, board: AtsBoard) -> dict:
    try:
        postings = read_board(board.provider, board.token)
    except Exception as exc:  # noqa: BLE001 - shown on the page
        board.last_result = {"error": str(exc)[:300]}
        db.commit()
        return {"source": board.company_name or board.token, "error": str(exc)[:200]}
    counts = import_board_postings(db, board, postings)
    return {"source": board.company_name or board.token, **counts}


@router.post("/{posting_id}/triage")
def triage(posting_id: int, action: str = Form(...), seg: str = Form("new"), db: Session = Depends(get_db)):
    """Keep -> joins the kept list; dismiss -> DISMISSED (undo: keep it from
    the closed tab)."""
    posting = _posting_or_404(db, posting_id)
    posting.inbox = False
    if action == "dismiss":
        posting.status = PostingStatus.DISMISSED
    elif action == "keep" and posting.status in (PostingStatus.DISMISSED, PostingStatus.EXPIRED):
        posting.status = PostingStatus.NEW
    db.commit()
    return _back(seg, posting_id)


@router.post("/bulk")
def triage_all(action: str = Form(...), seg: str = Form("new"), db: Session = Depends(get_db)):
    """keep_all / dismiss_all on the shown "new" list; dismiss_hidden on the
    current tab's postings that miss the filter."""
    rules = owner_filter(db)
    for posting in _segment_query(db, seg if seg in ("new", "kept") else "new").all():
        matches = check_posting(rules, posting)["match"]
        if action == "keep_all" and matches:
            posting.inbox = False
        elif action == "dismiss_all" and matches:
            posting.inbox, posting.status = False, PostingStatus.DISMISSED
        elif action == "dismiss_hidden" and not matches:
            posting.inbox, posting.status = False, PostingStatus.DISMISSED
    db.commit()
    return _back(seg)


@router.get("/boards")
def boards_page():
    return _back("sources")


@router.post("/boards")
def add_board(provider: str = Form(...), token: str = Form(...), company_name: str = Form(""), db: Session = Depends(get_db)):
    token = token.strip().strip("/")
    if provider in SUPPORTED_PROVIDERS and token:
        if db.query(AtsBoard).filter(AtsBoard.provider == provider, AtsBoard.token == token).first() is None:
            db.add(AtsBoard(provider=provider, token=token, company_name=company_name.strip() or token, found_via="manual"))
            db.commit()
    return _back("sources")


@router.post("/boards/discover")
def discover(db: Session = Depends(get_db)):
    try:
        result = discover_boards(db)
    except SearchNotConfigured as exc:
        return _back("sources", error=str(exc)[:200])
    return _back("sources", report=_encode_report([{"source": "search", "boards_found": len(result["new_boards"])}]))


def _board_or_404(db: Session, board_id: int) -> AtsBoard:
    board = db.get(AtsBoard, board_id)
    if board is None:
        raise NotFoundError(f"Board {board_id} not found")
    return board


@router.post("/boards/{board_id}/read")
def read_board_postings(board_id: int, db: Session = Depends(get_db)):
    """Read one board fresh: matches go to "new", the rest are counted by
    the rule they miss (shown in the report)."""
    line = _read_one_board(db, _board_or_404(db, board_id))
    return _back("new", report=_encode_report([line]))


@router.post("/boards/{board_id}/delete")
def delete_board(board_id: int, db: Session = Depends(get_db)):
    db.delete(_board_or_404(db, board_id))
    db.commit()
    return _back("sources")


@router.post("/careers-watch")
def careers_watch(db: Session = Depends(get_db), llm: LLMProvider = Depends(get_llm)):
    summary = watch_all(db, llm)
    line = {"source": "careers pages", "read": summary["checked"], "imported": summary["new_postings"], "changed": summary["changed"]}
    return _back("new", report=_encode_report([line]))


@router.post("/from-page/{company_id}")
def posting_from_page(company_id: int, url: str = Form(...), db: Session = Depends(get_db), llm: LLMProvider = Depends(get_llm)):
    """A job page research found on the company's own site -> a JobPosting
    (read fresh from that URL), linked to the company."""
    company = db.get(Company, company_id)
    if company is None:
        raise NotFoundError(f"Company {company_id} not found")
    try:
        posting, _ = add_posting(db, llm, url=url, source="careers_page")
    except ValueError as exc:
        return _back("kept", error=str(exc)[:300])
    if posting.company_id is None:
        posting.company_id = company.id
        posting.proposed_company = {}
        db.commit()
    return _back("kept", posting.id)


@router.post("/from-company/{company_id}")
def boards_for_company(company_id: int, db: Session = Depends(get_db)):
    company = db.get(Company, company_id)
    if company is not None:
        boards_from_research(db, company)
    return _back("sources")


@router.get("/{posting_id}")
def posting_detail(request: Request, posting_id: int, error: str = "", db: Session = Depends(get_db)):
    """The posting on its own (the postings page shows the same body in its
    right-hand pane)."""
    return render(templates, request, "posting_detail.html", _detail_context(db, _posting_or_404(db, posting_id), error))


@router.post("/{posting_id}/status")
def change_status(posting_id: int, status: str = Form(...), db: Session = Depends(get_db)):
    posting = _posting_or_404(db, posting_id)
    if status in PostingStatus.__members__:
        try:
            set_posting_status(db, posting, PostingStatus[status])
        except ValueError:
            pass
    return RedirectResponse(url=f"/postings/{posting_id}", status_code=303)


@router.post("/{posting_id}/evaluate")
def evaluate(posting_id: int, db: Session = Depends(get_db), llm: LLMProvider = Depends(get_llm)):
    posting = _posting_or_404(db, posting_id)
    if (posting.extra or {}).get("parsed") is False:
        parse_and_evaluate(db, llm, posting)
    else:
        evaluate_fit(db, llm, posting)
    return RedirectResponse(url=f"/postings/{posting_id}", status_code=303)


@router.post("/{posting_id}/expiry")
def expiry(posting_id: int, db: Session = Depends(get_db)):
    check_expiry(db, _posting_or_404(db, posting_id))
    return RedirectResponse(url=f"/postings/{posting_id}", status_code=303)


@router.post("/{posting_id}/approve-company")
def approve_company(posting_id: int, db: Session = Depends(get_db)):
    company = approve_proposed_company(db, _posting_or_404(db, posting_id))
    return RedirectResponse(url=f"/companies/{company.id}", status_code=303)


@router.post("/{posting_id}/confirm-hard")
def confirm_hard(posting_id: int, db: Session = Depends(get_db)):
    posting = _posting_or_404(db, posting_id)
    posting.hard_exclusion_confirmed = True
    db.commit()
    return RedirectResponse(url=f"/postings/{posting_id}", status_code=303)


@router.post("/{posting_id}/draft")
def draft_for_posting(
    posting_id: int,
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_draft_llm),
    review_llm: LLMProvider | None = Depends(get_review_llm),
):
    """Expiry is checked first (a re-fetch); a closed posting is never drafted."""
    posting = _posting_or_404(db, posting_id)
    if posting.company is None:
        return RedirectResponse(url=f"/postings/{posting_id}?error=company", status_code=303)
    if check_expiry(db, posting):
        return RedirectResponse(url=f"/postings/{posting_id}?error=expired", status_code=303)
    application = create_draft_application(db, llm, posting.company, review_llm=review_llm, job_posting=posting)
    if posting.status == PostingStatus.NEW:
        posting.status = PostingStatus.SHORTLISTED
        db.commit()
    return RedirectResponse(url=f"/applications/{application.id}", status_code=303)
