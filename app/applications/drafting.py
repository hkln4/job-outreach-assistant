"""Recommendation + drafting (CLAUDE.md section 5 steps 3-4). Evidence Rule: every
company-specific claim in the draft must be backed by a source URL from the
company's cached research."""
import json

from sqlalchemy.orm import Session

from app.llm.base import LLMProvider
from app.llm.json_utils import parse_json_object
from app.models import CV, Application, ApplicationState, Company, Profile

DRAFT_SYSTEM_PROMPT = """You are drafting a job application email for the owner, using \
their profile and researched company text.

Rules:
- Recommend ONE target position and give explicit written reasoning for the choice, \
grounded in the company text and the owner's target_roles/skills.
- If an OWNER'S CHOSEN POSITION is given below, use exactly that as target_position - \
don't substitute your own pick; your reasoning then explains the fit, not the choice.
- If an OWNER'S REASON FOR APPLYING is given, this is an open/speculative application: \
there may be no matching job posting at all. Never pretend one exists - write the email \
as a direct introduction ("I'm reaching out because..."), grounded in that reason, and \
set target_position to the owner's chosen position or a general role family (e.g. \
"Backend Engineer - open application").
- If OWNER'S NOTES ON WHY THIS COMPANY is given, weave that motivation into the "why \
this company" part - it reflects what actually drew the owner here. Company facts still \
need source URLs; the owner's own motivation does not.
- Recommend which CV variant fits best (choose from the given list by exact name) with \
reasoning.
- Write "reasoning" as a short note to the owner (not part of the email itself), in three \
short parts, in this order:
  1. One or two sentences on what the company actually does, grounded only in the given \
company text.
  2. Which of the owner's specific skills, projects, or experience (name them - don't just \
say "your background fits") make this a good match for this role, grounded only in the \
given profile summary/skills.
  3. The explicit recommendation: this position, with this CV, because <short reason>.
  Keep it compact - a few sentences per part, not an essay. Write it in the same language \
you write the email in.
- Write a company-specific cover-letter email. If the owner provided an OWNER'S COVER \
LETTER TEMPLATE below, closely follow ITS structure, section order, tone, length, and \
phrasing style - treat it as the owner's preferred voice, not just an example to glance at. \
Swap in this company's specific details (and the profile's) where the template has \
company/owner-specific content. If no template was provided, use this default structure:
  1. Greeting addressed to the company (or hiring team, if no contact name is known).
  2. Why this company - 1-2 sentences, grounded in specific, sourced facts about the \
company (not generic flattery).
  3. Why the owner - a short paragraph connecting the owner's profile/skills/experience \
to what the company needs, grounded in the profile.
  4. A closing line mentioning the attached CV and inviting next steps.
  5. Sign-off.
- EVERY company-specific claim (something you say about the company) must be backed by \
one of the given source URLs - do not state anything about the company that isn't in the \
given text.
- Ground personal/background claims only in the given profile summary/skills - never invent.
- Decide the email's language yourself: look at the language the company's researched text \
(website/job postings) is written in and write the subject and cover letter in that same \
language. If the company text is ambiguous or mixed, default to Turkish, since most of the \
companies the owner applies to are Turkish. Do not mix languages within the email.
- Output ONLY valid JSON, this exact shape:
{"target_position": string, "cv_choice_name": string, "reasoning": string, \
"subject": string, "cover_letter_body": string, "sources": [string]}
"""

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


def draft_application(
    db: Session,
    llm: LLMProvider,
    company: Company,
    feedback: str | None = None,
    target_position: str = "",
    application_reason: str = "",
) -> dict:
    profile = db.query(Profile).first()
    if profile is None:
        raise ValueError("No Profile yet - run the Profile Builder and synthesize first.")

    cvs = db.query(CV).all()
    if not cvs:
        raise ValueError("No CV variants configured - add at least one CV first.")

    company_text = "\n\n---\n\n".join(
        f"[{p['url']}]\n{p['text']}" for p in company.research.get("pages", [])
    ) or "(no research cached yet - run research first)"

    cv_list = "\n".join(f"- {cv.name} (target_role: {cv.target_role or 'unknown'})" for cv in cvs)

    prompt = (
        f"OWNER PROFILE SUMMARY:\n{profile.summary}\n\n"
        f"OWNER SKILLS:\n{json.dumps(profile.skills)}\n\n"
        f"OWNER TARGET ROLES:\n{', '.join(profile.target_roles)}\n\n"
        f"AVAILABLE CV VARIANTS:\n{cv_list}\n\n"
        f"COMPANY: {company.name}\n\nCOMPANY RESEARCH TEXT:\n{company_text[:24000]}"
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
    return data


def create_draft_application(
    db: Session,
    llm: LLMProvider,
    company: Company,
    target_position: str = "",
    application_reason: str = "",
) -> Application:
    result = draft_application(
        db, llm, company, target_position=target_position, application_reason=application_reason
    )
    cv = _resolve_cv(db, result.get("cv_choice_name", ""))

    application = Application(
        company_id=company.id,
        cv_id=cv.id if cv else None,
        target_position=target_position.strip() or result.get("target_position", ""),
        application_reason=application_reason.strip(),
        subject=result.get("subject", ""),
        cover_letter_body=result.get("cover_letter_body", ""),
        sources=result.get("sources", []),
        revision_history=[{"reasoning": result.get("reasoning", "")}],
        state=ApplicationState.PENDING_APPROVAL,
    )
    db.add(application)
    db.commit()
    db.refresh(application)
    return application


def regenerate_draft(db: Session, llm: LLMProvider, application: Application, feedback: str) -> Application:
    # Carry the owner's original intent (reason / open-application mode) into
    # every regeneration - feedback refines the email, not the reason for it.
    result = draft_application(
        db,
        llm,
        application.company,
        feedback=feedback,
        application_reason=application.application_reason,
    )
    cv = _resolve_cv(db, result.get("cv_choice_name", ""))

    # Index 0 always holds the CURRENT draft's reasoning (read by the AI-notes
    # panel); later entries are the feedback/previous_body history trail. Update
    # index 0 in place instead of just appending, or the AI notes would keep
    # showing the reasoning for a draft that's no longer the one on screen.
    history = list(application.revision_history) or [{"reasoning": ""}]
    history[0] = {**history[0], "reasoning": result.get("reasoning", "")}
    history.append({"feedback": feedback, "previous_body": application.cover_letter_body})
    application.revision_history = history

    application.target_position = result.get("target_position", application.target_position)
    if cv:
        application.cv_id = cv.id
    application.subject = result.get("subject", application.subject)
    application.cover_letter_body = result.get("cover_letter_body", application.cover_letter_body)
    application.sources = result.get("sources", application.sources)
    application.state = ApplicationState.PENDING_APPROVAL
    db.commit()
    db.refresh(application)
    return application
