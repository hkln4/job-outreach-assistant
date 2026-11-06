"""Recommendation + drafting (CLAUDE.md section 5 steps 3-4, roadmap phase 4).
Evidence Rule: every company-specific claim must be backed by a source URL from
the company's cached research (or the posting being answered).

A draft is three small calls on the draft model, each checked by code:
1. angles (app/applications/angles.py) - a quoted company hook + an owner
   record + a bridge; hooks not on a fetched page and ids not offered are
   dropped;
2. the letter as plain text (app/applications/letter.py), around the chosen
   angle, with style memory, recipient tone and the shared banned-phrase list;
3. metadata (sources, evidence used, CV choice, reasoning) over the finished
   letter.
The app then appends the signature block (never the model), links the lead
project's portfolio page when there is one, and runs the grounding audit
(numbers and technologies vs the sources) for "check before sending".
"""
import json
from urllib.parse import urldefrag

from sqlalchemy.orm import Session

from app.applications.angles import propose_angles, verify_angles
from app.applications.claims import check_claims
from app.applications.draft_review import review_draft
from app.applications.evidence import (
    EvidenceSelection,
    citable_text,
    evidence_prompt_section,
    evidence_records,
    items_by_ids,
    select_evidence,
)
from app.applications.grounding import grounding_misses
from app.applications.letter import letter_metadata, write_letter
from app.applications.review import ensure_not_sent
from app.applications.signature import owner_identity, signature_block, strip_trailing_name, with_signature
from app.applications.style import RECIPIENT_TONE, recipient_kind, style_prompt_section
from app.applications.synonyms import role_type_for
from app.companies.context import research_context
from app.companies.workflow import has_usable_research
from app.config import get_settings
from app.llm.base import LLMProvider
from app.llm.call_log import llm_context, set_llm_ids
from app.llm.untrusted import wrap_untrusted
from app.models import (
    CV,
    EMAIL_LANGUAGES,
    Application,
    ApplicationState,
    Company,
    JobPosting,
    Profile,
    ProfileItem,
    ProfileItemStatus,
    get_email_language,
)
from app.postings.gates import detect_language
from app.postings.service import has_unconfirmed_hard_flag

PROJECT_PAGE_LABEL = {"tr": "Proje sayfası", "en": "Project page"}

DRAFT_CONTEXT_CHARS = 24000
POSTING_CONTEXT_CHARS = 12000
# A posting with at least this much text can be drafted from even before the
# company itself is researched.
MIN_POSTING_CHARS = 200


def company_context_for(company: Company, job_posting: JobPosting | None, budget: int) -> str:
    """The company text a letter is grounded in: the posting being answered
    first (when there is one), then the research by priority."""
    if job_posting is None:
        return research_context(company.research, budget, with_header=True)
    posting_text = job_posting.description_text or ""
    if len(posting_text) > POSTING_CONTEXT_CHARS:
        posting_text = posting_text[: POSTING_CONTEXT_CHARS - 500] + "\n...\n" + posting_text[-500:]
    header = f"JOB POSTING BEING ANSWERED: {job_posting.title}\n[{job_posting.url or 'pasted posting'}]\n{posting_text}"
    rest = research_context(company.research, max(0, budget - len(header)), with_header=True) if has_usable_research(company) else ""
    return header + ("\n\n---\n\n" + rest if rest else "")


def _normalize_url(url: str) -> str:
    return urldefrag(url.strip())[0].rstrip("/").lower()


def validate_sources(sources: list[str], pages: list[dict]) -> tuple[list[str], list[str]]:
    """Evidence Rule, enforced by the app: a source only counts if it is one of
    the pages research actually fetched. Returns (kept, dropped); kept URLs are
    the fetched page's own spelling, deduped."""
    fetched = {_normalize_url(p["url"]): p["url"] for p in pages if p.get("url")}
    kept: list[str] = []
    dropped: list[str] = []
    for source in sources:
        page_url = fetched.get(_normalize_url(source))
        if page_url is None:
            dropped.append(source)
        elif page_url not in kept:
            kept.append(page_url)
    return kept, dropped


def _resolve_cv(db: Session, cv_choice_name: str) -> CV | None:
    """CLAUDE.md section 9: a CV must always be attached. The LLM picks a name
    from a list we gave it, but LLM output is never guaranteed to match a DB
    string byte-for-byte, so match case/whitespace-insensitively too - an exact
    match failing here means "no CV attached" (silent per section 9), which is
    worse than a normalization miss."""
    cvs = db.query(CV).all()
    if not cvs:
        return None

    normalized_choice = (cv_choice_name or "").strip().casefold()
    for cv in cvs:
        if cv.name.strip().casefold() == normalized_choice:
            return cv

    if len(cvs) == 1:
        return cvs[0]  # nothing else it could have meant

    return None


def _require_draft_inputs(db: Session, company: Company, job_posting: JobPosting | None = None) -> tuple[Profile, list[CV]]:
    profile = db.query(Profile).first()
    if profile is None:
        raise ValueError("No Profile yet - run the Profile Builder and synthesize first.")

    cvs = db.query(CV).all()
    if not cvs:
        raise ValueError("No CV variants configured - add at least one CV first.")

    # Without researched text the model has nothing to ground a company-specific
    # letter in (Evidence Rule): it either invents one or answers with a request
    # for the missing information, which then fails as "invalid JSON". Say what
    # is actually wrong instead.
    if job_posting is not None:
        if (job_posting.status.value if job_posting.status else "") == "EXPIRED":
            raise ValueError("This posting is closed (expired) - nothing to apply to.")
        if has_unconfirmed_hard_flag(job_posting):
            raise ValueError("The posting matches a hard exclusion - confirm it on the posting page before drafting.")
        if len((job_posting.description_text or "").strip()) >= MIN_POSTING_CHARS:
            return profile, cvs  # the posting itself is the company text
    if not has_usable_research(company):
        raise ValueError(
            f"Research for {company.name} came back empty - there is no site text to write from. "
            "Re-run research, or fix the website URL (some sites only render in a browser)."
        )
    return profile, cvs


def letter_language(db: Session, profile: Profile, job_posting: JobPosting | None) -> str:
    """tr | en: the owner's choice, else the posting's language, else the
    template's, else Turkish (CLAUDE.md section 1)."""
    chosen = get_email_language(db)
    if chosen:
        return chosen
    if job_posting is not None and job_posting.language in EMAIL_LANGUAGES:
        return job_posting.language
    if (profile.cover_letter_template or "").strip():
        return detect_language(profile.cover_letter_template) or "tr"
    return "tr"


def portfolio_link_for(db: Session, item: ProfileItem | None, portfolio_url: str) -> str:
    """The owner's portfolio page for the lead project, when one was ingested:
    the item itself if it is a portfolio page, else a portfolio sub-page whose
    URL or text names the repo."""
    if item is None or not portfolio_url:
        return ""
    root = portfolio_url.rstrip("/")
    if item.source_type == "site" and item.source_ref.startswith(root) and item.source_ref.rstrip("/") != root:
        return item.source_ref
    if item.source_type != "github_repo":
        return ""
    repo = item.source_ref.split("/")[-1].casefold()
    for page in db.query(ProfileItem).filter(ProfileItem.source_type == "site", ProfileItem.status == ProfileItemStatus.ANALYZED).all():
        if not page.source_ref.startswith(root) or page.source_ref.rstrip("/") == root:
            continue
        text = f"{page.source_ref} {(page.extracted or {}).get('purpose', '')} {' '.join((page.extracted or {}).get('highlights') or [])}".casefold()
        if repo and (repo in text or repo.replace("-", " ") in text):
            return page.source_ref
    return ""


def _sources_text(profile: Profile, evidence_items: list[ProfileItem], company_text: str) -> str:
    """Everything a letter may draw facts from - for the grounding audit."""
    # Only citable record text counts - model framing (role angles, talking
    # points) must not make a number or technology look sourced.
    parts = [profile.summary or "", json.dumps(profile.skills or [], ensure_ascii=False), company_text]
    parts += [citable_text(item) for item in evidence_items]
    return "\n".join(parts)


@llm_context("draft")
def draft_application(
    db: Session,
    llm: LLMProvider,
    company: Company,
    feedback: str | None = None,
    target_position: str = "",
    application_reason: str = "",
    evidence_items: list[ProfileItem] | None = None,
    job_posting: JobPosting | None = None,
    angles: list[dict] | None = None,
    angle_index: int = 0,
    application: Application | None = None,
) -> dict:
    """One draft. `angles` reuses saved angles (no angle call); `angle_index`
    picks which one the letter is built around."""
    set_llm_ids(company_id=company.id)
    profile, cvs = _require_draft_inputs(db, company, job_posting)
    evidence_items = evidence_items or []

    pages = list((company.research or {}).get("pages", []))
    # Budgeted by priority: job pages first, then careers/about/home/contact,
    # with the research summary and open positions on top (section 8). A
    # posting being answered leads the context.
    company_text = company_context_for(company, job_posting, DRAFT_CONTEXT_CHARS)
    if job_posting is not None and job_posting.url:
        pages.append({"url": job_posting.url, "kind": "job", "text": job_posting.description_text})

    research = company.research or {}
    role_type = role_type_for(
        " ".join([target_position, application_reason, research.get("summary") or ""]
                 + [p.get("title", "") for p in research.get("open_positions") or []]
                 + ([job_posting.title, " ".join(job_posting.requirements or [])] if job_posting else []))
    )
    evidence_section = evidence_prompt_section(evidence_items, role_type)
    cv_list = "\n".join(f"- {cv.name} (target_role: {cv.target_role or 'unknown'})" for cv in cvs)
    language = letter_language(db, profile, job_posting)

    owner_context = (
        f"OWNER PROFILE SUMMARY:\n{profile.summary}\n\n"
        f"OWNER SKILLS:\n{', '.join(s.get('skill', '') for s in profile.skills or [])}\n\n"
        f"OWNER TARGET ROLES:\n{', '.join(profile.target_roles)}"
    )
    if target_position.strip():
        owner_context += f"\n\nOWNER'S CHOSEN POSITION (use exactly this):\n{target_position.strip()}"
    if application_reason.strip():
        owner_context += f"\n\nOWNER'S REASON FOR APPLYING (open/speculative application):\n{application_reason.strip()}"
    if company.why_interested.strip():
        owner_context += f"\n\nOWNER'S NOTES ON WHY THIS COMPANY:\n{company.why_interested.strip()}"

    # 1. Angles (kept from earlier drafts when given)
    offered = {item.id for item in evidence_items}
    dropped_angles = 0
    if angles is None:
        raw = propose_angles(llm, company_name=company.name, company_text=company_text, evidence_section=evidence_section, owner_context=owner_context)
        angles, dropped_angles = verify_angles(raw, pages, offered)
    angle_index = angle_index if 0 <= angle_index < len(angles) else 0
    angle = angles[angle_index] if angles else None

    # 2. The letter, plain text
    prompt = (
        f"{owner_context}\n\nRETRIEVED PROFILE EVIDENCE (the owner's most relevant background records):\n"
        f"{evidence_section or 'none'}\n\nCOMPANY: {company.name}\n\nCOMPANY RESEARCH TEXT:\n{wrap_untrusted(company_text)}"
    )
    if angle:
        prompt += (
            f"\n\nCHOSEN ANGLE:\n- company hook: \"{angle['company_hook']}\" ({angle['hook_url']})\n"
            f"- lead evidence: E{angle['owner_evidence_id']}\n- bridge: {angle['bridge']}\n- position: {angle['position']}"
        )
    chosen_language = get_email_language(db)
    if chosen_language:
        prompt += f"\n\nOWNER'S PREFERRED EMAIL LANGUAGE:\n{EMAIL_LANGUAGES[chosen_language]}"
    elif job_posting is not None and job_posting.language in EMAIL_LANGUAGES:
        prompt += f"\n\nJOB POSTING LANGUAGE:\n{EMAIL_LANGUAGES[job_posting.language]}"
    if profile.cover_letter_template.strip():
        prompt += f"\n\nOWNER'S COVER LETTER TEMPLATE (follow its structure/tone closely):\n{profile.cover_letter_template.strip()}"
    kind = recipient_kind(db, application, company)
    if RECIPIENT_TONE.get(kind):
        prompt += f"\n\nRECIPIENT:\n{RECIPIENT_TONE[kind]}"
    style = style_prompt_section(db, language, "")
    if style:
        prompt += f"\n\n{style}"
    if feedback:
        prompt += f"\n\nOWNER FEEDBACK ON PREVIOUS DRAFT (revise accordingly):\n{feedback}"
    letter = write_letter(llm, prompt)

    # 3. Metadata over the finished letter (the writer may have returned it already)
    meta = letter["metadata"] or letter_metadata(
        llm,
        subject=letter["subject"],
        body=letter["body"],
        context=(
            f"AVAILABLE CV VARIANTS:\n{cv_list}\n\nPROFILE EVIDENCE:\n{evidence_section or 'none'}\n\n"
            f"COMPANY TEXT:\n{wrap_untrusted(company_text)}"
        ),
    )
    # Sentence-level evidence: every claim's quote is looked up in its source by code.
    claim_check = (
        check_claims(letter["body"], meta["claims"], pages, evidence_items, company.name)
        if meta.get("claims")
        else {"not_checked": True}
    )
    sources, dropped_sources = validate_sources(list(meta.get("sources") or []), pages)
    sources += [v["source"] for r in claim_check.get("sentences", []) for v in r["supports"] if v["kind"] == "page" and v["source"] not in sources]
    if angle and angle["hook_url"] not in sources:
        sources.append(angle["hook_url"])
    used_ids = [int(i) for i in dict.fromkeys(meta.get("evidence_item_ids") or []) if str(i).isdigit() and int(i) in offered]
    if angle and angle["owner_evidence_id"] not in used_ids:
        used_ids.insert(0, angle["owner_evidence_id"])

    # App-written parts: project page link + signature block
    identity = owner_identity(db)
    lead_item = next((item for item in evidence_items if angle and item.id == angle["owner_evidence_id"]), None)
    project_page = portfolio_link_for(db, lead_item, identity.get("portfolio_url", ""))
    body = strip_trailing_name(letter["body"], identity.get("full_name", ""))
    if project_page and project_page not in body:
        body = f"{body.rstrip()}\n\n{PROJECT_PAGE_LABEL.get(language, 'Project page')}: {project_page}"
    signature = signature_block(identity, language)
    body = with_signature(body, signature)

    misses = grounding_misses(letter["body"], _sources_text(profile, evidence_items, company_text))
    return {
        "target_position": meta.get("target_position") or (angle or {}).get("position", ""),
        "cv_choice_name": meta.get("cv_choice_name", ""),
        "reasoning": meta.get("reasoning", ""),
        "subject": letter["subject"],
        "cover_letter_body": body,
        "sources": sources,
        "dropped_sources": dropped_sources,
        "evidence_item_ids": used_ids,
        "unsupported_claims": list(meta.get("unsupported_claims") or []),
        "angles": angles,
        "angle_index": angle_index,
        "dropped_angles": dropped_angles,
        "recipient_kind": kind,
        "grounding_misses": misses,
        "project_page": project_page,
        "metadata_error": meta.get("metadata_error", ""),
        "claim_check": claim_check,
    }


def _draft_notes(result: dict, selection: EvidenceSelection) -> dict:
    """Index 0 of revision_history: notes about the CURRENT draft, read by the
    review page (AI reasoning + what the app had to correct or fall back on)."""
    return {
        "reasoning": result.get("reasoning", ""),
        "dropped_sources": result["dropped_sources"],
        "unsupported_claims": result["unsupported_claims"],
        "evidence_fallback": selection.fallback,
        "evidence_error": selection.error,
        "angles": result.get("angles") or [],
        "angle_index": result.get("angle_index", 0),
        "dropped_angles": result.get("dropped_angles", 0),
        "recipient_kind": result.get("recipient_kind", ""),
        "grounding_misses": result.get("grounding_misses") or {},
        "project_page": result.get("project_page", ""),
        "metadata_error": result.get("metadata_error", ""),
        "claim_check": result.get("claim_check") or {},
    }


def _review_notes(
    db: Session, review_llm: LLMProvider | None, company: Company, result: dict, selection: EvidenceSelection
) -> dict:
    """The reviewer pass (app/applications/draft_review.py) as a notes entry,
    or nothing when it is off or no reviewer was supplied."""
    if review_llm is None or not get_settings().draft_review_enabled:
        return {}
    profile = db.query(Profile).first()
    email_language = get_email_language(db)
    review = review_draft(
        review_llm,
        company=company,
        subject=result.get("subject", ""),
        body=result.get("cover_letter_body", ""),
        evidence_items=selection.items,
        template=profile.cover_letter_template if profile else "",
        language=EMAIL_LANGUAGES.get(email_language, ""),
    )
    return {"review": review}


@llm_context()
def create_draft_application(
    db: Session,
    llm: LLMProvider,
    company: Company,
    target_position: str = "",
    application_reason: str = "",
    review_llm: LLMProvider | None = None,
    job_posting: JobPosting | None = None,
) -> Application:
    """`review_llm` (the cheap reading model) runs the independent reviewer
    pass on the new draft; None skips it. With `job_posting`, the letter
    answers that posting: its title is the position and its text leads."""
    set_llm_ids(company_id=company.id)
    if job_posting is not None and not target_position.strip():
        target_position = job_posting.title
    _require_draft_inputs(db, company, job_posting)  # fail before spending a call on evidence selection
    selection = select_evidence(db, llm, company, target_position, application_reason, job_posting=job_posting)
    result = draft_application(
        db,
        llm,
        company,
        target_position=target_position,
        application_reason=application_reason,
        evidence_items=selection.items,
        job_posting=job_posting,
    )
    cv = _resolve_cv(db, result.get("cv_choice_name", ""))

    application = Application(
        company_id=company.id,
        cv_id=cv.id if cv else None,
        job_posting_id=job_posting.id if job_posting is not None else None,
        target_position=target_position.strip() or result.get("target_position", ""),
        application_reason=application_reason.strip(),
        subject=result.get("subject", ""),
        cover_letter_body=result.get("cover_letter_body", ""),
        sources=result["sources"],
        evidence=evidence_records(selection.items, selection.why, result["evidence_item_ids"]),
        revision_history=[{**_draft_notes(result, selection), **_review_notes(db, review_llm, company, result, selection)}],
        state=ApplicationState.PENDING_APPROVAL,
    )
    db.add(application)
    db.commit()
    db.refresh(application)
    from app.cv_builder.service import auto_tailor  # local: cv_builder imports drafting helpers

    auto_tailor(db, application)
    return application


def _evidence_for_regeneration(db: Session, llm: LLMProvider, application: Application) -> EvidenceSelection:
    """Reuse the saved selection (no extra LLM call per regenerate, section 8)
    unless the owner excluded one of its records - then pick again without it."""
    excluded = application.excluded_evidence_ids or []
    saved = application.evidence or []
    saved_ids = [entry["item_id"] for entry in saved]
    if saved_ids and not set(saved_ids) & set(excluded):
        items = items_by_ids(db, saved_ids)
        if items:
            notes = (application.revision_history or [{}])[0]
            return EvidenceSelection(
                items=items,
                why={entry["item_id"]: entry.get("why", "") for entry in saved},
                fallback=bool(notes.get("evidence_fallback")),
                error=notes.get("evidence_error", ""),
            )
    return select_evidence(
        db,
        llm,
        application.company,
        application.target_position,
        application.application_reason,
        exclude_ids=excluded,
        job_posting=application.job_posting,
    )


@llm_context()
def regenerate_draft(
    db: Session,
    llm: LLMProvider,
    application: Application,
    feedback: str,
    review_llm: LLMProvider | None = None,
    angle_index: int | None = None,
) -> Application:
    """Feedback refines the letter; `angle_index` rebuilds it around another
    of the saved angles. Saved angles are reused - no new angle call."""
    set_llm_ids(company_id=application.company_id, application_id=application.id)
    ensure_not_sent(application)
    saved_notes = (application.revision_history or [{}])[0]
    saved_angles = saved_notes.get("angles") if "angles" in saved_notes else None
    if angle_index is None:
        angle_index = saved_notes.get("angle_index", 0)
    _require_draft_inputs(db, application.company, application.job_posting)
    selection = _evidence_for_regeneration(db, llm, application)
    # Carry the owner's original intent (position, reason / open-application
    # mode) into every regeneration - feedback refines the email, not what it
    # applies for. A different position is set through the edit form.
    result = draft_application(
        db,
        llm,
        application.company,
        feedback=feedback,
        target_position=application.target_position,
        application_reason=application.application_reason,
        evidence_items=selection.items,
        job_posting=application.job_posting,
        angles=saved_angles,
        angle_index=angle_index,
        application=application,
    )
    cv = _resolve_cv(db, result.get("cv_choice_name", ""))

    # Index 0 always holds the CURRENT draft's reasoning (read by the AI-notes
    # panel); later entries are the feedback/previous_body history trail. Update
    # index 0 in place instead of just appending, or the AI notes would keep
    # showing the reasoning for a draft that's no longer the one on screen.
    history = list(application.revision_history) or [{"reasoning": ""}]
    # The old review described the old draft - drop it, then review the new one.
    notes = {key: value for key, value in history[0].items() if key != "review"}
    history[0] = {
        **notes,
        **_draft_notes(result, selection),
        **_review_notes(db, review_llm, application.company, result, selection),
    }
    history.append({"feedback": feedback, "previous_body": application.cover_letter_body, "angle_index": angle_index})
    application.revision_history = history

    if not application.target_position.strip():
        application.target_position = result.get("target_position", "")
    if cv:
        application.cv_id = cv.id
    application.subject = result.get("subject", application.subject)
    application.cover_letter_body = result.get("cover_letter_body", application.cover_letter_body)
    application.sources = result["sources"]
    application.evidence = evidence_records(selection.items, selection.why, result["evidence_item_ids"])
    application.state = ApplicationState.PENDING_APPROVAL
    db.commit()
    db.refresh(application)
    return application


def refresh_grounding(db: Session, application: Application) -> None:
    """Re-run the grounding audit after the owner edited the letter (or applied
    a reviewer edit), so "check before sending" describes the current text."""
    profile = db.query(Profile).first()
    if profile is None or not application.revision_history:
        return
    items = items_by_ids(db, [entry["item_id"] for entry in application.evidence or []])
    company_text = company_context_for(application.company, application.job_posting, DRAFT_CONTEXT_CHARS)
    history = list(application.revision_history)
    notes = {**history[0], "grounding_misses": grounding_misses(application.cover_letter_body, _sources_text(profile, items, company_text))}
    if notes.get("claim_check") and not notes["claim_check"].get("not_checked"):
        notes["claim_check"] = {**notes["claim_check"], "stale": True}  # the text changed since the check
    history[0] = notes
    application.revision_history = history


def letter_core(db: Session, application: Application) -> str:
    """The letter without the app-written tail (signature block, project page
    line) - those are the owner's own details, not claims to verify."""
    identity = owner_identity(db)
    profile = db.query(Profile).first()
    language = letter_language(db, profile, application.job_posting) if profile else "tr"
    body = application.cover_letter_body or ""
    signature = signature_block(identity, language)
    if signature and body.rstrip().endswith(signature):
        body = body.rstrip()[: -len(signature)].rstrip()
    labels = tuple(f"{label}:" for label in PROJECT_PAGE_LABEL.values())
    return "\n".join(line for line in body.splitlines() if not line.strip().startswith(labels)).strip()


def recheck_claims(db: Session, llm: LLMProvider, application: Application) -> dict:
    """Run the sentence-level check again on the letter as it is now (after
    the owner's edits): one metadata call, verified by code."""
    company = application.company
    items = items_by_ids(db, [entry["item_id"] for entry in application.evidence or []])
    pages = list((company.research or {}).get("pages", []))
    if application.job_posting is not None and application.job_posting.url:
        pages.append({"url": application.job_posting.url, "kind": "job", "text": application.job_posting.description_text})
    company_text = company_context_for(company, application.job_posting, DRAFT_CONTEXT_CHARS)
    body = letter_core(db, application)
    meta = letter_metadata(
        llm,
        subject=application.subject,
        body=body,
        context=f"PROFILE EVIDENCE:\n{evidence_prompt_section(items) or 'none'}\n\nCOMPANY TEXT:\n{wrap_untrusted(company_text)}",
    )
    result = check_claims(body, meta.get("claims") or [], pages, items, company.name) if meta.get("claims") else {"not_checked": True}
    history = list(application.revision_history or [{}])
    history[0] = {**history[0], "claim_check": result}
    application.revision_history = history
    db.commit()
    return result


def known_records_text(db: Session, application: Application) -> str:
    """What the profile already says, for fact capture to compare against."""
    profile = db.query(Profile).first()
    items = items_by_ids(db, [entry["item_id"] for entry in application.evidence or []])
    return _sources_text(profile, items, "") if profile else ""
