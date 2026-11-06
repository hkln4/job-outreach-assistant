"""Job postings UI (roadmap phase 3): add a posting (URL or pasted text), see
its gates, fit and legitimacy flags, match its company, draft an application
for it; read public ATS boards; check careers pages for new postings. Every
action is a click - nothing runs in the background."""
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.applications.drafting import create_draft_application
from app.companies.linkedin_fetch import linkedin_job_id
from app.config import get_settings
from app.db import get_db
from app.llm.base import LLMProvider
from app.models import AtsBoard, Company, JobPosting, PostingStatus, Preference
from app.postings.ats import SUPPORTED_PROVIDERS, boards_from_research, import_board_postings, read_board
from app.postings.careers_watch import watch_all
from app.postings.evaluation import evaluate_fit
from app.postings.gates import is_relevant, owner_targets, role_gate
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


@router.get("")
def postings_index(request: Request, status: str = "open", error: str = "", watch: str = "", show_all: int = 0, db: Session = Depends(get_db)):
    """Relevant postings first; ones the role gate rejected and weak fits are
    folded away (never deleted)."""
    query = db.query(JobPosting)
    if status == "open":
        query = query.filter(JobPosting.status.in_([PostingStatus.NEW, PostingStatus.SHORTLISTED]))
    elif status in PostingStatus.__members__:
        query = query.filter(JobPosting.status == PostingStatus[status])
    everything = sorted(
        query.all(),
        key=lambda p: (VERDICT_ORDER.get((p.fit or {}).get("verdict", "unknown"), 2), -(p.id or 0)),
    )
    def hidden(p: JobPosting) -> bool:
        return not is_relevant(p.gates or {}) or (p.fit or {}).get("verdict") in ("weak", "blocked")
    postings = everything if show_all else [p for p in everything if not hidden(p)]
    hidden_count = len(everything) - len(postings)
    return render(
        templates,
        request,
        "postings.html",
        {
            "postings": postings,
            "hidden_count": hidden_count,
            "show_all": bool(show_all),
            "filter_status": status,
            "statuses": list(PostingStatus),
            "error": error,
            "watch": watch.split(",") if watch.count(",") == 3 else None,
            "linkedin_enabled": get_settings().linkedin_research_enabled,
        },
    )


@router.post("")
def create_posting(
    url: str = Form(""),
    text: str = Form(""),
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_llm),
):
    try:
        posting, _ = add_posting(db, llm, url=url.strip(), text=text, source="linkedin" if linkedin_job_id(url) else "manual")
    except ValueError as exc:
        return RedirectResponse(url=f"/postings?error={quote(str(exc)[:300])}", status_code=303)
    return RedirectResponse(url=f"/postings/{posting.id}", status_code=303)


@router.post("/dismiss-irrelevant")
def dismiss_irrelevant(db: Session = Depends(get_db)):
    """One click: every open posting the role gate rejects (or whose fit was
    weak / blocked) -> DISMISSED. The owner triggers it; nothing is deleted."""
    for posting in db.query(JobPosting).filter(JobPosting.status.in_([PostingStatus.NEW, PostingStatus.SHORTLISTED])).all():
        targets, skills = owner_targets(db)
        posting.gates = {**(posting.gates or {}), "role": role_gate(posting.title, posting.description_text, targets, skills)}
        if not is_relevant(posting.gates) or (posting.fit or {}).get("verdict") in ("weak", "blocked"):
            posting.status = PostingStatus.DISMISSED
    db.commit()
    return RedirectResponse(url="/postings", status_code=303)


@router.get("/boards")
def boards_page(request: Request, discovered: str = "", error: str = "", db: Session = Depends(get_db)):
    settings = get_settings()
    return render(
        templates,
        request,
        "posting_boards.html",
        {
            "boards": db.query(AtsBoard).order_by(AtsBoard.id.desc()).all(),
            "providers": SUPPORTED_PROVIDERS,
            "search_enabled": settings.search_api_enabled and bool(settings.search_api_key),
            "discovered": discovered,
            "error": error,
        },
    )


@router.post("/boards")
def add_board(provider: str = Form(...), token: str = Form(...), company_name: str = Form(""), db: Session = Depends(get_db)):
    token = token.strip().strip("/")
    if provider in SUPPORTED_PROVIDERS and token:
        if db.query(AtsBoard).filter(AtsBoard.provider == provider, AtsBoard.token == token).first() is None:
            db.add(AtsBoard(provider=provider, token=token, company_name=company_name.strip() or token, found_via="manual"))
            db.commit()
    return RedirectResponse(url="/postings/boards", status_code=303)


@router.post("/boards/discover")
def discover(db: Session = Depends(get_db)):
    try:
        result = discover_boards(db)
    except SearchNotConfigured as exc:
        return RedirectResponse(url=f"/postings/boards?error={quote(str(exc)[:200])}", status_code=303)
    return RedirectResponse(url=f"/postings/boards?discovered={len(result['new_boards'])}", status_code=303)


def _board_or_404(db: Session, board_id: int) -> AtsBoard:
    board = db.get(AtsBoard, board_id)
    if board is None:
        raise NotFoundError(f"Board {board_id} not found")
    return board


@router.post("/boards/{board_id}/read")
async def read_board_postings(request: Request, board_id: int, db: Session = Depends(get_db)):
    """Read the board fresh; accepted (Turkey) postings are imported, ambiguous
    ones are listed for the owner to pick (submit again with their ids)."""
    board = _board_or_404(db, board_id)
    form = await request.form()
    include = {str(v) for v in form.getlist("include_ids")}
    try:
        postings = read_board(board.provider, board.token)
    except Exception as exc:  # noqa: BLE001 - shown on the page
        board.last_result = {"error": str(exc)[:300]}
        db.commit()
        return RedirectResponse(url="/postings/boards", status_code=303)
    counts = import_board_postings(db, board, postings, include)
    ambiguous = [p for p in postings if p["location_gate"]["result"] in ("ambiguous", "unknown") and p["external_id"] not in include]
    return render(templates, request, "posting_board_read.html", {"board": board, "counts": counts, "ambiguous": ambiguous})


@router.post("/careers-watch")
def careers_watch(db: Session = Depends(get_db), llm: LLMProvider = Depends(get_llm)):
    summary = watch_all(db, llm)
    values = ",".join(str(summary[k]) for k in ("checked", "changed", "new_postings", "skipped"))
    return RedirectResponse(url=f"/postings?watch={values}", status_code=303)


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
        return RedirectResponse(url=f"/postings?error={quote(str(exc)[:300])}", status_code=303)
    if posting.company_id is None:
        posting.company_id = company.id
        posting.proposed_company = {}
        db.commit()
    return RedirectResponse(url=f"/postings/{posting.id}", status_code=303)


@router.post("/from-company/{company_id}")
def boards_for_company(company_id: int, db: Session = Depends(get_db)):
    company = db.get(Company, company_id)
    if company is not None:
        boards_from_research(db, company)
    return RedirectResponse(url="/postings/boards", status_code=303)


@router.get("/{posting_id}")
def posting_detail(request: Request, posting_id: int, error: str = "", db: Session = Depends(get_db)):
    posting = _posting_or_404(db, posting_id)
    preferences = {p.id: p for p in db.query(Preference).all()}
    copies = [db.get(JobPosting, c["id"]) for c in (posting.extra or {}).get("copies", [])]
    return render(
        templates,
        request,
        "posting_detail.html",
        {
            "posting": posting,
            "preferences": preferences,
            "hard_blocked": has_unconfirmed_hard_flag(posting),
            "statuses": list(PostingStatus),
            "copies": [c for c in copies if c is not None],
            "error": error,
        },
    )


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
