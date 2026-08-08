"""Per-company research orchestration (CLAUDE.md section 5 step 2): fetch, run the
preference check, and report the application channel as feedback only."""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.companies.fetch import fetch_company_research
from app.email_send.gmail_client import validate_email
from app.llm.base import LLMProvider
from app.llm.json_utils import parse_json_object
from app.models import Company, Preference
from app.preferences.checker import check_company

CHANNEL_SYSTEM_PROMPT = """Based on this company website text (home page + careers/about/\
contact pages if available), do four things:
1. Write a short summary (2-4 sentences) of what this company actually does - its product/\
service, industry, and anything notable about hiring/open roles if mentioned. Base it ONLY \
on the given text - never invent or assume anything not stated, and never rely on the \
company's name alone. If the text is too thin to say anything real, say so plainly instead \
of padding with generic filler.
2. Report - as feedback only, not authoritative - whether the company appears to mention an \
online application form, LinkedIn, or kariyer.net for job applications.
3. Look for an email address to send a job application to (a careers/HR/jobs/recruitment \
address, or a general contact address if that's the only one given). Only report an email \
that is literally present in the given text - never guess or construct one (e.g. do not \
invent info@<domain> if it isn't actually written on the page). If none is present, use null.
4. List individual people literally named in the text (team/about/management pages) who \
could be relevant for a job application - founders, CTO, engineering leads, HR/recruiting. \
For each: name, role as written, and email ONLY if that person's own email is literally in \
the text (else null). Never guess or construct names, roles, or emails. Empty list if none.
Output ONLY valid JSON, this exact shape:
{"summary": string, "channels": [string], "contact_email": string | null, \
"people": [{"name": string, "role": string, "email": string | null}]}
"""

CHANNEL_FALLBACK = {"summary": "", "channels": [], "contact_email": None, "people": []}


def research_company(db: Session, llm: LLMProvider, company: Company) -> Company:
    if not company.website_url:
        raise ValueError("Company has no website_url set - cannot research.")

    result = fetch_company_research(company.website_url, company.careers_url)
    combined_text = "\n\n---\n\n".join(f"[{p['url']}]\n{p['text']}" for p in result["pages"])

    channel_response = llm.generate(
        prompt=combined_text[:24000], system=CHANNEL_SYSTEM_PROMPT, temperature=0.0, max_output_tokens=600
    )
    channel_data = parse_json_object(channel_response.text, fallback=CHANNEL_FALLBACK)

    preferences = db.query(Preference).all()
    flags = check_company(llm, combined_text, preferences)
    pref_by_id = {p.id: p for p in preferences}

    primary_source_url = result["pages"][0]["url"] if result["pages"] else company.website_url
    preference_check = []
    for flag in flags:
        pref = pref_by_id.get(flag.get("preference_id"))
        if pref is None:
            continue
        preference_check.append(
            {
                "preference_id": pref.id,
                "matched": True,
                "severity": pref.type.value,
                "evidence_snippet": flag.get("evidence_snippet", ""),
                "source_url": primary_source_url,
                "confidence": flag.get("confidence", "low"),
            }
        )

    # Contact suggestions (evidence-first): only people literally named on the
    # fetched pages, surfaced in the UI for the owner to confirm into Contacts.
    # No LinkedIn scraping, no enrichment providers (those stay v2).
    suggested_contacts = []
    for person in channel_data.get("people", []) or []:
        name = (person.get("name") or "").strip()
        if not name:
            continue
        email = (person.get("email") or "").strip()
        suggested_contacts.append(
            {
                "name": name,
                "role": (person.get("role") or "").strip(),
                "email": email if validate_email(email) else "",
                "source_url": primary_source_url,
            }
        )

    company.research = {
        "pages": result["pages"],
        "summary": channel_data.get("summary", ""),
        "suggested_contacts": suggested_contacts,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    company.other_channels = channel_data.get("channels", [])
    company.preference_check = preference_check
    company.hard_exclusion_confirmed = False  # new research invalidates any prior confirmation
    if result.get("detected_careers_url") and not company.careers_url:
        company.careers_url = result["detected_careers_url"]

    # Auto-fill contact_email from the researched text, but never overwrite an
    # email the owner already entered themselves - the owner's own input is
    # always the final word, this is just a fallback so filling it in by hand
    # is optional, not required.
    found_email = channel_data.get("contact_email")
    if not company.contact_email and found_email and validate_email(found_email):
        company.contact_email = found_email.strip()

    db.commit()
    db.refresh(company)
    return company


def research_company_safe(db: Session, llm: LLMProvider, company: Company) -> Company:
    """Same as research_company, but a failure (site unreachable, blocked, LLM
    error, ...) is recorded on the company instead of raising - so a bad fetch
    shows up as a visible "research failed: <reason>" on the company page
    instead of either crashing the request or silently leaving it blank."""
    try:
        return research_company(db, llm, company)
    except Exception as exc:  # noqa: BLE001 - surfaced on the company page, not swallowed
        company.research = {"pages": [], "error": str(exc)}
        db.commit()
        db.refresh(company)
        return company


def has_unconfirmed_hard_exclusion(company: Company) -> bool:
    has_hard = any(pc["severity"] == "hard" for pc in company.preference_check)
    return has_hard and not company.hard_exclusion_confirmed
