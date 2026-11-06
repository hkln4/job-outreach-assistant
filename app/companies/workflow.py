"""Per-company research orchestration (CLAUDE.md section 5 step 2): fetch, run the
preference check, and report the application channel as feedback only.

Research answers, from the fetched pages only:
- what the company actually does (detailed enough to decide which position fits)
- careers: is there a careers page, what's on it, is there an on-site application
  form or an external application portal (informational - email stays the channel)
- every application-relevant email on the site; the APP (not the model) then
  prioritizes HR/recruitment addresses over generic ones, since HR runs hiring
- a profile-fit suggestion (which position to target and why), grounded in the
  owner's profile - preferences are checked separately by the preference checker
"""
import hashlib
import re
from datetime import datetime, timezone

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.companies.context import build_company_context, locate_quote
from app.companies.emails import merge_emails
from app.companies.fetch import MIN_USABLE_RESEARCH_CHARS, fetch_company_research
from app.email_send.gmail_client import validate_email
from app.llm.base import LLMProvider
from app.llm.call_log import llm_context, set_llm_ids
from app.llm.json_utils import parse_json_object
from app.llm.untrusted import UNTRUSTED_RULE, wrap_untrusted
from app.models import Company, Preference, Profile
from app.preferences.checker import PREFERENCE_CONTEXT_CHARS, check_company

RESEARCH_SYSTEM_PROMPT = """You are researching a company's website text (home page + \
careers/about/contact pages if available) on behalf of a job applicant. Base EVERYTHING \
only on the given text - never invent, assume, or rely on the company's name or reputation. \
If the text is too thin to say something, say so plainly (or use null/empty) instead of padding.

Report, as JSON:
1. "summary": what the company actually does - product/service, industry/domain, and any \
technologies, team, or customer types the text mentions. Detailed enough that the applicant \
can judge which kind of position to target. 3-6 sentences.
1b. "summary_evidence": for EACH sentence of the summary, {"sentence": the summary sentence, \
"quote": the words on the site that state it (copied exactly, character for character), \
"url": the [url] header of the page the quote is on}. A sentence you can't back with a quote \
should not be in the summary.
2. "open_positions": job openings literally listed in the text. For each: "title" as written \
and "details" (a short quote/paraphrase of requirements/stack if given, else ""). Empty list if none.
3. "careers": {"has_careers_page": bool (does the text include a careers/jobs section?), \
"application_form": bool (does it mention applying via an on-site form?), \
"external_portal": string|null (URL or name of an external application portal - e.g. \
kariyer.net, LinkedIn jobs, Workable, Lever - if the text points there), \
"notes": string (anything else notable about how they hire, else "")}.
4. "channels": short feedback list of application channels mentioned (e.g. "form", \
"linkedin", "kariyer.net"). Empty list if none.
5. "emails": EVERY email address literally present in the text, each as {"email": string, \
"context": string (the surrounding words identifying what it's for, e.g. "HR contact", \
"general info", as written)}. Never guess or construct an address (no inventing info@domain). \
Empty list if none.
6. "people": individuals literally named in the text (team/about/management) relevant for a \
job application - founders, CTO, engineering leads, HR/recruiting. For each: "name", "role" \
as written, "email" ONLY if that person's own email is literally in the text (else null). \
Never guess. Empty list if none.
7. "fit": ONLY if an OWNER PROFILE section is provided: {"recommended_position": the open \
position title that best matches the profile, or if none are listed, the role/team the owner \
should target in an open application (string|null), "reasoning": 2-4 sentences comparing \
what the company does/needs (from the text) with the owner's skills and target roles \
(from the profile). Ground every company-side claim in the text and every owner-side claim \
in the profile.} If no profile section is given, use null.

Output ONLY valid JSON, this exact shape:
{"summary": string, "summary_evidence": [{"sentence": string, "quote": string, "url": string}], \
"open_positions": [{"title": string, "details": string}], \
"careers": {"has_careers_page": bool, "application_form": bool, "external_portal": string | null, \
"notes": string}, "channels": [string], "emails": [{"email": string, "context": string}], \
"people": [{"name": string, "role": string, "email": string | null}], \
"fit": {"recommended_position": string | null, "reasoning": string} | null}
"""

RESEARCH_CONTEXT_CHARS = 40000


class _SummaryEvidence(BaseModel):
    sentence: str
    quote: str
    url: str = ""


class _Position(BaseModel):
    title: str
    details: str = ""


class _Careers(BaseModel):
    has_careers_page: bool = False
    application_form: bool = False
    external_portal: str | None = None
    notes: str = ""


class _Email(BaseModel):
    email: str
    context: str = ""


class _Person(BaseModel):
    name: str
    role: str = ""
    email: str | None = None


class _Fit(BaseModel):
    recommended_position: str | None = None
    reasoning: str = ""


# Answer shape, sent as the structured-output schema. The parsed dict is still
# read defensively below - the schema is the request, not a guarantee.
class ResearchAnswer(BaseModel):
    summary: str = ""
    summary_evidence: list[_SummaryEvidence] = []
    open_positions: list[_Position] = []
    careers: _Careers = _Careers()
    channels: list[str] = []
    emails: list[_Email] = []
    people: list[_Person] = []
    fit: _Fit | None = None


RESEARCH_FALLBACK = {
    "summary": "",
    "open_positions": [],
    "careers": {},
    "channels": [],
    "emails": [],
    "people": [],
    "fit": None,
}

# HR/recruitment address detection (deterministic, in the app - the model only
# extracts what's literally on the site). Local part is normalized to lowercase
# alphanumerics first, so "insan.kaynaklari@x" -> "insankaynaklari".
HR_LOCAL_EXACT = {"hr", "ik", "cv", "jobs", "job", "kariyer", "career", "careers"}
HR_LOCAL_SUBSTRINGS = (
    "kariyer",
    "career",
    "job",
    "recruit",
    "insankaynak",
    "humanresource",
    "hiring",
    "talent",
    "personel",
    "basvuru",
    "staffing",
)


def classify_email(email: str) -> str:
    """"hr" for HR/recruitment-looking addresses, "general" otherwise."""
    local = email.split("@")[0]
    normalized = re.sub(r"[^a-z0-9]", "", local.lower())
    if normalized in HR_LOCAL_EXACT:
        return "hr"
    if any(s in normalized for s in HR_LOCAL_SUBSTRINGS):
        return "hr"
    return "general"


def pick_contact_email(emails: list[dict]) -> dict | None:
    """First valid HR/recruitment address wins; otherwise the first valid one.
    HR runs hiring, so applications should land there when possible."""
    valid = [e for e in emails if validate_email((e.get("email") or "").strip()) and not e.get("invalid")]
    for e in valid:
        if e["kind"] == "hr":
            return e
    return valid[0] if valid else None


def _profile_context(db: Session) -> str:
    profile = db.query(Profile).first()
    if profile is None or not profile.summary.strip():
        return ""
    skills = ", ".join(s.get("skill", "") for s in profile.skills if s.get("skill"))
    return (
        "\n\nOWNER PROFILE (for the fit assessment only):\n"
        f"SUMMARY: {profile.summary.strip()}\n"
        f"TARGET ROLES: {', '.join(profile.target_roles)}\n"
        f"SKILLS: {skills}"[:4000]
    )


@llm_context("research")
def research_company(db: Session, llm: LLMProvider, company: Company, force: bool = False) -> Company:
    """Fetch + summarize + preference check. When the fetched text is identical
    to the last research (content hash) the previous AI answers are reused and
    no LLM call is made, unless `force` (section 8: deterministic first);
    emails, channels and links are always recomputed by code."""
    if not company.website_url:
        raise ValueError("Company has no website_url set - cannot research.")
    set_llm_ids(company_id=company.id)

    result = fetch_company_research(company.website_url, company.careers_url)

    # Nothing readable came back (JS-only site, a shell behind a WAF, wrong
    # URL). Record that plainly instead of storing an empty-but-successful
    # research that later makes drafting ask the model to write about a blank
    # page - which is what produced "invalid JSON" failures at draft time.
    content_chars = result.get("content_chars")
    if content_chars is None:  # derive it rather than depend on the fetch reporting it
        content_chars = sum(len(p.get("text") or "") for p in result["pages"])

    if content_chars < MIN_USABLE_RESEARCH_CHARS:
        company.research = {
            "pages": result["pages"],
            "content_chars": content_chars,
            "content_safety": result.get("content_safety"),
            # Code-found addresses and channels still count on a page with
            # too little text to research (a JS shell with a mailto footer).
            "emails": _emails_for(company, result.get("emails_detected") or [], []),
            "channels_detected": result.get("channels_detected") or [],
            "empty": True,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        if result.get("detected_linkedin_url") and not company.linkedin_url:
            company.linkedin_url = result["detected_linkedin_url"]
        db.commit()
        db.refresh(company)
        return company

    previous = company.research or {}
    digest = hashlib.sha256("\n".join(p.get("text") or "" for p in result["pages"]).encode()).hexdigest()
    preferences = db.query(Preference).all()
    preference_ids = sorted(p.id for p in preferences)
    reuse = (
        not force
        and previous.get("content_hash") == digest
        and previous.get("llm_answer") is not None
        and previous.get("llm_preference_ids") == preference_ids
    )
    if reuse:
        data, flags = previous["llm_answer"], previous.get("llm_flags") or []
    else:
        # 40k chars (~10k tokens), allocated by priority so the job-posting pages
        # are never the part that gets cut (app/companies/context.py).
        research_text = build_company_context(result["pages"], RESEARCH_CONTEXT_CHARS)
        response = llm.generate(
            prompt=wrap_untrusted(research_text) + _profile_context(db),
            system=RESEARCH_SYSTEM_PROMPT + "\n" + UNTRUSTED_RULE,
            temperature=0.0,
            max_output_tokens=1500,
            json_schema=ResearchAnswer.model_json_schema(),
        )
        data = parse_json_object(response.text, fallback=RESEARCH_FALLBACK)
        flags = check_company(llm, build_company_context(result["pages"], PREFERENCE_CONTEXT_CHARS), preferences)
    pref_by_id = {p.id: p for p in preferences}

    pages = result["pages"]
    primary_source_url = pages[0]["url"] if pages else company.website_url
    preference_check = []
    for flag in flags:
        pref = pref_by_id.get(flag.get("preference_id"))
        if pref is None:
            continue
        snippet = flag.get("evidence_snippet", "")
        # The page the snippet is actually on, found by code; the homepage
        # only when no page contains it - and then marked unverified.
        located = locate_quote(snippet, pages)
        preference_check.append(
            {
                "preference_id": pref.id,
                "matched": True,
                "severity": pref.type.value,
                "evidence_snippet": snippet,
                "source_url": located or primary_source_url,
                "source_url_verified": located is not None,
                "confidence": flag.get("confidence", "low"),
            }
        )

    # Contact suggestions (evidence-first): only people literally named on the
    # fetched pages, surfaced in the UI for the owner to confirm into Contacts.
    # No LinkedIn scraping, no enrichment providers (those stay v2).
    suggested_contacts = []
    for person in data.get("people", []) or []:
        name = (person.get("name") or "").strip()
        if not name:
            continue
        email = (person.get("email") or "").strip()
        suggested_contacts.append(
            {
                "name": name,
                "role": (person.get("role") or "").strip(),
                "email": email if validate_email(email) else "",
                "source_url": locate_quote(name, pages) or primary_source_url,
            }
        )

    # All emails on the site: code-found ones from the raw HTML first, then
    # any extra the model reported; HR/recruitment classified by the app.
    emails = _emails_for(company, result.get("emails_detected") or [], data.get("emails", []) or [])

    careers = data.get("careers") or {}
    company.research = {
        "pages": result["pages"],
        "content_chars": content_chars,
        # What the sanitizer stripped/flagged before the LLM saw the text -
        # the company page warns when it looks aimed at an AI reader.
        "content_safety": result.get("content_safety"),
        "summary": data.get("summary", ""),
        # Only summary sentences backed by a quote found on a page (by code)
        # reach later prompts; open positions only when the title is on a page.
        "summary_verified": verify_summary(data.get("summary_evidence") or [], result["pages"]),
        "open_positions": verify_positions(data.get("open_positions", []) or [], result["pages"]),
        "careers": {
            "has_careers_page": bool(careers.get("has_careers_page")),
            # The model's guess, kept only as a fallback note - the real
            # answer is `channels_detected`, read from the HTML by code.
            "application_form": bool(careers.get("application_form")),
            "external_portal": careers.get("external_portal") or "",
            "notes": careers.get("notes") or "",
            # app-side detection, not the model's: a near-empty careers page is
            # almost certainly JS-rendered, so the owner should eyeball it
            "page_looks_thin": bool(result.get("careers_page_thin")),
        },
        "emails": emails,
        "channels_detected": result.get("channels_detected") or [],
        "fit": data.get("fit") or None,
        "suggested_contacts": suggested_contacts,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        # For the next re-run: same text + same preferences -> reuse, no LLM call.
        "content_hash": digest,
        "llm_answer": data if "parse_error" not in data else None,
        "llm_flags": flags,
        "llm_preference_ids": preference_ids,
        "reused_llm": reuse,
    }
    company.other_channels = data.get("channels", [])
    company.preference_check = preference_check
    if not reuse:
        company.hard_exclusion_confirmed = False  # new findings invalidate any prior confirmation
    if result.get("detected_careers_url") and not company.careers_url:
        company.careers_url = result["detected_careers_url"]
    # Most sites link their own LinkedIn page from the header/footer, so the
    # owner rarely has to type it - but an owner-entered URL always wins.
    if result.get("detected_linkedin_url") and not company.linkedin_url:
        company.linkedin_url = result["detected_linkedin_url"]

    # Auto-fill contact_email from the researched text (HR-first), but never
    # overwrite an email the owner already entered themselves - the owner's own
    # input is always the final word, this is just a fallback so filling it in
    # by hand is optional, not required.
    chosen = pick_contact_email(emails)
    if not company.contact_email and chosen is not None:
        company.contact_email = chosen["email"].strip()

    db.commit()
    db.refresh(company)
    return company


def _emails_for(company: Company, detected: list[dict], from_llm: list[dict]) -> list[dict]:
    """Merged, classified emails; addresses that bounced before are kept but
    marked, so pick_contact_email never offers them again."""
    invalid = {e.lower() for e in company.invalid_emails or []}
    emails = []
    for entry in merge_emails(detected, from_llm):
        address = entry["email"]
        emails.append(
            {
                "email": address,
                "context": (entry.get("context") or "").strip(),
                "url": entry.get("url") or "",
                "found_by": entry.get("found_by", "llm"),
                "kind": classify_email(address),
                "invalid": address.lower() in invalid,
            }
        )
    return emails


def verify_summary(evidence: list[dict], pages: list[dict]) -> list[dict]:
    """Summary sentences whose quote is on a fetched page: [{sentence, quote, url}]."""
    verified = []
    for entry in evidence:
        quote = (entry.get("quote") or "").strip()
        if len(quote.split()) < 2:
            continue
        page_url = locate_quote(quote, pages)
        if page_url and entry.get("sentence"):
            verified.append({"sentence": entry["sentence"].strip(), "quote": quote, "url": page_url})
    return verified


def verify_positions(positions: list[dict], pages: list[dict]) -> list[dict]:
    """Each position marked verified (with the page) when its title is on a page."""
    out = []
    for position in positions:
        title = (position.get("title") or "").strip()
        page_url = locate_quote(title, pages) if title else None
        out.append({**position, "verified": page_url is not None, "url": page_url or ""})
    return out


def research_company_safe(db: Session, llm: LLMProvider, company: Company, force: bool = False) -> Company:
    """Same as research_company, but a failure (site unreachable, blocked, LLM
    error, ...) is recorded on the company instead of raising - so a bad fetch
    shows up as a visible "research failed: <reason>" on the company page
    instead of either crashing the request or silently leaving it blank."""
    try:
        return research_company(db, llm, company, force=force)
    except Exception as exc:  # noqa: BLE001 - surfaced on the company page, not swallowed
        company.research = {"pages": [], "error": str(exc)}
        db.commit()
        db.refresh(company)
        return company


def has_usable_research(company: Company) -> bool:
    """Whether there is enough researched text to write a grounded draft from.

    A fetch can succeed and still yield nothing (JS-only site, empty shell),
    and drafting from that produces either an invented cover letter or a model
    that answers with a request for information instead of JSON. Everything
    that offers drafting - the company page, the dashboard queue, the drafting
    call itself - gates on this.
    """
    research = company.research or {}
    if research.get("error") or research.get("empty"):
        return False
    pages = research.get("pages") or []
    if not pages:
        return False
    chars = research.get("content_chars")
    if chars is None:  # researched before this check existed
        chars = sum(len(p.get("text") or "") for p in pages)
    return chars >= MIN_USABLE_RESEARCH_CHARS


def has_unconfirmed_hard_exclusion(company: Company) -> bool:
    has_hard = any(pc["severity"] == "hard" for pc in company.preference_check)
    return has_hard and not company.hard_exclusion_confirmed
