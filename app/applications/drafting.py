"""Recommendation + drafting (CLAUDE.md section 5 steps 3-4). Evidence Rule: every
company-specific claim in the draft must be backed by a source URL from the
company's cached research."""
import json
from urllib.parse import urldefrag

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy.orm import Session

from app.applications.evidence import (
    EvidenceSelection,
    evidence_prompt_section,
    evidence_records,
    items_by_ids,
    select_evidence,
)
from app.applications.review import ensure_not_sent
from app.companies.workflow import has_usable_research
from app.llm.base import LLMProvider
from app.llm.json_utils import parse_json_object
from app.llm.untrusted import UNTRUSTED_RULE, wrap_untrusted
from app.models import CV, Application, ApplicationState, Company, Profile, ProfileItem

DRAFT_SYSTEM_PROMPT = """You are writing a job application email on behalf of the owner
(a new graduate), using their profile, the most relevant background records, and the
company's own website text.

Work in this order:

1. Position
If OWNER'S CHOSEN POSITION is given, use it exactly as target_position. Otherwise,
choose the role that best fits the company's open positions and the owner's target roles.
If OWNER'S REASON FOR APPLYING is given, treat this as an open application: do not imply
there is an open posting unless the company text explicitly says so.

2. The company
Identify what the company actually builds, for whom, and the concrete problem it solves.
Only use facts stated in the company text. Do not infer responsibilities for the role unless
they are explicitly described in a job posting.

3. The owner's relevant matches
Review RETRIEVED PROFILE EVIDENCE and keep only experience that is genuinely relevant:
the same problem, domain, or technology used for the same purpose. Sharing a language,
framework, or generic AI interest alone is not enough.

a. Lead project
Choose ONE experience or project that best fits the position. A GitHub project can be
selected over CV experience if it is more relevant.

Describe it in at most two short sentences:
- what problem the owner worked on;
- what they built;
- optionally, one concrete result or metric only if it strongly supports relevance.

Do not list datasets, model variants, libraries, infrastructure, or several metrics unless
they are essential to understanding the project's relevance.

b. Supporting sentence
Add at most one short sentence about another relevant project, skill, or technology.
Omit this sentence entirely when it does not make the application more relevant.
Never mention unrelated projects just to show breadth.

4. Write the email

If OWNER'S COVER LETTER TEMPLATE is given, follow its overall tone, greeting, and closing,
but replace company- and project-specific content with the relevant information above.

Without a template, write four short paragraphs:
1. Who the owner is and which position they seek.
2. One factual, concrete reason for contacting this company.
3. The lead project in one or two sentences, followed by an optional supporting sentence.
4. A short closing that mentions the attached CV and asks for an opportunity to talk.

Email requirements:
- Aim for 130–180 words in Turkish or 100–150 words in English.
- This is an introductory email, not a CV summary or technical project report.
- Prefer relevance and clarity over completeness.
- Use short, direct, natural sentences.
- If OWNER'S NOTES ON WHY THIS COMPANY is given, use it for the motivation paragraph.
- Language: use OWNER'S PREFERRED EMAIL LANGUAGE when provided. Otherwise use the
  language of OWNER'S COVER LETTER TEMPLATE; if neither is available, use Turkish.
- Never mix languages within the email.
- Subject: "Junior <target_position> Başvurusu" in Turkish or "Junior <target_position> Application"
  in English. Do not add "Junior" if target_position already includes it.

Tone:
- Write like a new graduate writing to a person, not a generic cover-letter template.
- Explain the company plainly; do not repeat product names, acronyms, or jargon unless
  necessary and briefly explained.
- Do not praise the company or claim interest that is not supported by the owner's notes.
- Never use stock phrases such as:
  "katkı sağlayabileceğime inanıyorum",
  "ilgiyle takip ediyorum",
  "bilgi ve deneyimlerimle örtüşüyor",
  "sağlam bir temel oluşturuyor",
  "I believe I can contribute",
  "I have been following with great interest".
- Show evidence instead of making broad claims. Do not say the owner has "strong"
  experience; say what they built when it is relevant.
- Do not invent confidence, experience, motivation, or qualifications.

Evidence rules:
- Every company statement must come from the company text.
- "sources" must contain only URLs copied exactly from [url] headers in the company text.
- Personal claims must come only from the profile summary, skills, and evidence records.
- Mention a technology only when evidence shows the owner used it.
- Include the number of every evidence record used in "evidence_item_ids".
- Put any personal claim not supported by evidence in "unsupported_claims"
  (normally this must be empty).

Also return:
- "cv_choice_name": the CV variant, using its exact name from the provided list, that best
  fits this position.
- "reasoning": a short note to the owner in the email's language:
  (1) what the company does;
  (2) which project was selected and why it is relevant;
  (3) why this position and CV were chosen.

Output ONLY valid JSON in this exact shape:
{"target_position": string, "cv_choice_name": string, "reasoning": string,
"subject": string, "cover_letter_body": string, "sources": [string],
"evidence_item_ids": [int], "unsupported_claims": [string]}
""" + UNTRUSTED_RULE

class DraftResult(BaseModel):
    """Shape check on the model's answer - a missing letter or a wrong-typed
    field becomes a readable error instead of a half-empty draft."""

    target_position: str = ""
    cv_choice_name: str = ""
    reasoning: str = ""
    subject: str
    cover_letter_body: str
    sources: list[str] = []
    evidence_item_ids: list[int] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)

    @field_validator("evidence_item_ids", mode="before")
    @classmethod
    def _ids_from_labels(cls, value: object) -> object:
        # The prompt labels records "[E12]"; models sometimes echo the label.
        if isinstance(value, list):
            return [str(v).strip().lstrip("Ee") if isinstance(v, str) else v for v in value]
        return value

    @field_validator("subject", "cover_letter_body")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value


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


DRAFT_FALLBACK = {
    "target_position": "unknown",
    "cv_choice_name": "unknown",
    "reasoning": "",
    "subject": "",
    "cover_letter_body": "",
    "sources": [],
}


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


def _require_draft_inputs(db: Session, company: Company) -> tuple[Profile, list[CV]]:
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
    if not has_usable_research(company):
        raise ValueError(
            f"Research for {company.name} came back empty - there is no site text to write from. "
            "Re-run research, or fix the website URL (some sites only render in a browser)."
        )
    return profile, cvs


def draft_application(
    db: Session,
    llm: LLMProvider,
    company: Company,
    feedback: str | None = None,
    target_position: str = "",
    application_reason: str = "",
    evidence_items: list[ProfileItem] | None = None,
) -> dict:
    profile, cvs = _require_draft_inputs(db, company)

    pages = company.research.get("pages", [])
    company_text = "\n\n---\n\n".join(f"[{p['url']}]\n{p['text']}" for p in pages)

    cv_list = "\n".join(f"- {cv.name} (target_role: {cv.target_role or 'unknown'})" for cv in cvs)

    prompt = (
        f"OWNER PROFILE SUMMARY:\n{profile.summary}\n\n"
        f"OWNER SKILLS:\n{json.dumps(profile.skills)}\n\n"
        f"OWNER TARGET ROLES:\n{', '.join(profile.target_roles)}\n\n"
        f"AVAILABLE CV VARIANTS:\n{cv_list}\n\n"
        f"RETRIEVED PROFILE EVIDENCE (the owner's most relevant background records):\n"
        f"{evidence_prompt_section(evidence_items or []) or 'none'}\n\n"
        f"COMPANY: {company.name}\n\nCOMPANY RESEARCH TEXT:\n{wrap_untrusted(company_text[:24000])}"
    )
    if target_position.strip():
        prompt += f"\n\nOWNER'S CHOSEN POSITION (use exactly this):\n{target_position.strip()}"
    if application_reason.strip():
        prompt += f"\n\nOWNER'S REASON FOR APPLYING (open/speculative application):\n{application_reason.strip()}"
    if company.why_interested.strip():
        prompt += f"\n\nOWNER'S NOTES ON WHY THIS COMPANY:\n{company.why_interested.strip()}"
    if profile.cover_letter_template.strip():
        prompt += f"\n\nOWNER'S COVER LETTER TEMPLATE (follow its structure/tone closely):\n{profile.cover_letter_template.strip()}"
    if feedback:
        prompt += f"\n\nOWNER FEEDBACK ON PREVIOUS DRAFT (revise accordingly):\n{feedback}"

    response = llm.generate(prompt=prompt, system=DRAFT_SYSTEM_PROMPT, temperature=0.3, max_output_tokens=4096)
    data = parse_json_object(response.text, fallback=DRAFT_FALLBACK)
    if "parse_error" in data:
        raise ValueError(
            "Drafting produced invalid JSON (likely truncated - try again). "
            f"Raw response start:\n{data['parse_error']}"
        )
    try:
        result = DraftResult.model_validate(data).model_dump()
    except ValidationError as exc:
        fields = ", ".join(".".join(str(part) for part in err["loc"]) for err in exc.errors())
        raise ValueError(f"Drafting returned an incomplete draft (bad fields: {fields}) - try again.") from exc

    result["sources"], result["dropped_sources"] = validate_sources(result["sources"], pages)
    given_ids = {item.id for item in evidence_items or []}
    result["evidence_item_ids"] = [i for i in dict.fromkeys(result["evidence_item_ids"]) if i in given_ids]
    return result


def _draft_notes(result: dict, selection: EvidenceSelection) -> dict:
    """Index 0 of revision_history: notes about the CURRENT draft, read by the
    review page (AI reasoning + what the app had to correct or fall back on)."""
    return {
        "reasoning": result.get("reasoning", ""),
        "dropped_sources": result["dropped_sources"],
        "unsupported_claims": result["unsupported_claims"],
        "evidence_fallback": selection.fallback,
        "evidence_error": selection.error,
    }


def create_draft_application(
    db: Session,
    llm: LLMProvider,
    company: Company,
    target_position: str = "",
    application_reason: str = "",
) -> Application:
    _require_draft_inputs(db, company)  # fail before spending a call on evidence selection
    selection = select_evidence(db, llm, company, target_position, application_reason)
    result = draft_application(
        db,
        llm,
        company,
        target_position=target_position,
        application_reason=application_reason,
        evidence_items=selection.items,
    )
    cv = _resolve_cv(db, result.get("cv_choice_name", ""))

    application = Application(
        company_id=company.id,
        cv_id=cv.id if cv else None,
        target_position=target_position.strip() or result.get("target_position", ""),
        application_reason=application_reason.strip(),
        subject=result.get("subject", ""),
        cover_letter_body=result.get("cover_letter_body", ""),
        sources=result["sources"],
        evidence=evidence_records(selection.items, selection.why, result["evidence_item_ids"]),
        revision_history=[_draft_notes(result, selection)],
        state=ApplicationState.PENDING_APPROVAL,
    )
    db.add(application)
    db.commit()
    db.refresh(application)
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
    )


def regenerate_draft(db: Session, llm: LLMProvider, application: Application, feedback: str) -> Application:
    ensure_not_sent(application)
    _require_draft_inputs(db, application.company)
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
    )
    cv = _resolve_cv(db, result.get("cv_choice_name", ""))

    # Index 0 always holds the CURRENT draft's reasoning (read by the AI-notes
    # panel); later entries are the feedback/previous_body history trail. Update
    # index 0 in place instead of just appending, or the AI notes would keep
    # showing the reasoning for a draft that's no longer the one on screen.
    history = list(application.revision_history) or [{"reasoning": ""}]
    history[0] = {**history[0], **_draft_notes(result, selection)}
    history.append({"feedback": feedback, "previous_body": application.cover_letter_body})
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
