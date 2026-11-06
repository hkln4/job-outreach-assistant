"""
Entities and state machines, per CLAUDE.md sections 6 and 7.

JSON-shaped fields (skills[], source_urls[], research, preference_check[], etc.)
are stored as SQLite JSON columns rather than normalized child tables - this is a
single-user app and those fields are always read/written as a whole per item.
"""
import enum
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- State machines (section 7) ---


class CompanyState(str, enum.Enum):
    APPLICABLE = "APPLICABLE"
    ON_HOLD = "ON_HOLD"
    REJECTED = "REJECTED"


class ApplicationState(str, enum.Enum):
    # Approve+send moves PENDING_APPROVAL straight to AWAITING_REPLY (there is no
    # separate SENT stop), and reject/regenerate loops back to PENDING_APPROVAL.
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    AWAITING_REPLY = "AWAITING_REPLY"
    REPLIED = "REPLIED"
    INTERVIEW = "INTERVIEW"
    REJECTED = "REJECTED"
    NO_RESPONSE = "NO_RESPONSE"
    # Delivery failed (mailer-daemon / delivery-status report). The address is
    # recorded as invalid on the company and the owner is offered another one.
    BOUNCED = "BOUNCED"


class FollowUpState(str, enum.Enum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    SENT = "SENT"


class ProfileItemStatus(str, enum.Enum):
    ANALYZED = "analyzed"
    SKIPPED = "skipped"
    NEEDS_REVIEW = "needs_review"


class PreferenceType(str, enum.Enum):
    HARD = "hard"
    SOFT = "soft"


class CandidateStatus(str, enum.Enum):
    PENDING = "PENDING"  # imported from a directory, waiting for the owner
    APPROVED = "APPROVED"  # owner approved - now (or already) a Company
    REJECTED = "REJECTED"  # owner rejected - never suggested again
    MATCHED = "MATCHED"  # turned out to be a company already in the list - hidden


class ContactState(str, enum.Enum):
    NOT_CONTACTED = "NOT_CONTACTED"
    DRAFT = "DRAFT"
    SENT = "SENT"
    REPLIED = "REPLIED"
    NO_RESPONSE = "NO_RESPONSE"


# --- Entities (section 6) ---


class Profile(Base):
    """The owner's durable, reviewed profile - one row, built by the Profile Builder."""

    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    skills: Mapped[list] = mapped_column(JSON, default=list)  # [{skill, evidence_source}]
    target_roles: Mapped[list] = mapped_column(JSON, default=list)  # [str]
    # Optional owner-written example cover letter. When set, drafting follows its
    # structure/tone/format instead of the app's generic default structure.
    cover_letter_template: Mapped[str] = mapped_column(Text, default="")
    # Long "about me" document generated from all records, edited by the owner
    # (app/profile_builder/writeup.py). Never sent anywhere.
    writeup: Mapped[str] = mapped_column(Text, default="")
    writeup_generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    writeup_unknown_tags: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class ProfileItem(Base):
    """One per source item ingested by the Profile Builder (repo / CV / page / LinkedIn)."""

    __tablename__ = "profile_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_type: Mapped[str] = mapped_column(String)  # "github_repo" | "cv" | "linkedin" | "site"
    source_ref: Mapped[str] = mapped_column(String)  # repo full_name, file path, or URL
    extracted: Mapped[dict] = mapped_column(JSON, default=dict)  # {purpose, stack, role, highlights}
    status: Mapped[ProfileItemStatus] = mapped_column(
        Enum(ProfileItemStatus), default=ProfileItemStatus.NEEDS_REVIEW
    )
    source_urls: Mapped[list] = mapped_column(JSON, default=list)
    # Deterministic facts computed by code, not the model (repo manifests,
    # tests, CI, commit stats - app/profile_builder/repo_facts.py).
    facts: Mapped[dict] = mapped_column(JSON, default=dict)
    # What the record was built from: {pushed_at, head_sha, analyzed_at}.
    source_version: Mapped[dict] = mapped_column(JSON, default=dict)
    # Owner answers to the record's open_questions: [{question, answer, source: "owner", at}].
    owner_facts: Mapped[list] = mapped_column(JSON, default=list)
    # Fields the owner edited by hand - re-analysis never overwrites them.
    owner_edited_fields: Mapped[list] = mapped_column(JSON, default=list)
    # Re-analysis proposals for owner-edited fields / a re-analysis diff awaiting review.
    pending_update: Mapped[dict] = mapped_column(JSON, default=dict)
    # For one entry of a CV/LinkedIn file: the file it came from.
    parent_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Preference(Base):
    """Owner constraint checked against every company during research (section 4)."""

    __tablename__ = "preferences"

    id: Mapped[int] = mapped_column(primary_key=True)
    description: Mapped[str] = mapped_column(Text)
    type: Mapped[PreferenceType] = mapped_column(Enum(PreferenceType))
    signals: Mapped[list] = mapped_column(JSON, default=list)  # optional keywords/notes


# Language codes the owner can pick for outgoing letters, with the name the
# drafting prompt uses. "" (not listed) = no preference: follow the cover
# letter template's language, else Turkish.
EMAIL_LANGUAGES = {"tr": "Turkish", "en": "English"}


class OwnerSettings(Base):
    """Single-row owner settings that aren't facts about the owner (those live
    on Profile) - kept apart so saving one doesn't create an empty Profile."""

    __tablename__ = "owner_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    email_language: Mapped[str] = mapped_column(String, default="")
    # Identity for the signature block and the CV contact row - written by the
    # app, never by a model. Empty fields fall back to what the CV says
    # (app/applications/signature.py::owner_identity).
    full_name: Mapped[str] = mapped_column(String, default="")
    title: Mapped[str] = mapped_column(String, default="")
    email: Mapped[str] = mapped_column(String, default="")
    phone: Mapped[str] = mapped_column(String, default="")
    city: Mapped[str] = mapped_column(String, default="")
    linkedin_url: Mapped[str] = mapped_column(String, default="")
    github_url: Mapped[str] = mapped_column(String, default="")
    portfolio_url: Mapped[str] = mapped_column(String, default="")
    # Generated CVs (phase 5): the CV whose Education / Experience / Certificates
    # are copied verbatim, and whether the owner approved the template.
    base_cv_id: Mapped[int | None] = mapped_column(nullable=True)
    # Repos the owner deleted from his profile: never listed or suggested again.
    excluded_repos: Mapped[list] = mapped_column(JSON, default=list)
    # The posting filter (app/postings/filter.py): roles, excluded words,
    # levels, home city, remote-only elsewhere. Empty = the defaults.
    posting_filter: Mapped[dict] = mapped_column(JSON, default=dict)
    cv_template_approved: Mapped[bool] = mapped_column(default=False)


def get_email_language(db) -> str:
    """The owner's chosen letter language code, or "" for no preference."""
    settings = db.query(OwnerSettings).first()
    language = settings.email_language if settings else ""
    return language if language in EMAIL_LANGUAGES else ""


class CV(Base):
    """A CV variant available for attachment to applications."""

    __tablename__ = "cvs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    target_role: Mapped[str] = mapped_column(String, default="")
    file_path: Mapped[str] = mapped_column(String)
    notes: Mapped[str] = mapped_column(Text, default="")


class Company(Base):
    """A manually curated target company (section 5 step 1)."""

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    website_url: Mapped[str | None] = mapped_column(String, nullable=True)
    domain: Mapped[str | None] = mapped_column(String, nullable=True)
    careers_url: Mapped[str | None] = mapped_column(String, nullable=True)
    # Auto-detected from the website's own LinkedIn link during research; the
    # owner can fill it in or override it by hand when the site has no link.
    linkedin_url: Mapped[str | None] = mapped_column(String, nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String, nullable=True)
    liveness: Mapped[dict] = mapped_column(JSON, default=dict)  # same shape as CompanyCandidate.liveness
    # Addresses that bounced (ApplicationState.BOUNCED) - never offered again.
    invalid_emails: Mapped[list] = mapped_column(JSON, default=list)
    state: Mapped[CompanyState] = mapped_column(Enum(CompanyState), default=CompanyState.APPLICABLE)
    # Owner-entered context: why this company is on the list at all. Injected
    # into the drafting prompt so the cover letter reflects the owner's actual
    # motivation instead of only what the website says.
    why_interested: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(String, default="")  # e.g. "Ankara"
    technopark: Mapped[str] = mapped_column(String, default="")  # e.g. "ODTÜ Teknokent"
    other_channels: Mapped[list] = mapped_column(JSON, default=list)  # feedback-only, e.g. ["form", "linkedin"]
    research: Mapped[dict] = mapped_column(JSON, default=dict)  # cached extracted text + findings w/ source URLs
    # LinkedIn findings (headcount, roles, people, activity, jobs). Kept apart
    # from `research` on purpose: the two are refreshed by separate owner
    # actions, and a failed website research overwrites `research` wholesale.
    linkedin_research: Mapped[dict] = mapped_column(JSON, default=dict)
    preference_check: Mapped[list] = mapped_column(JSON, default=list)
    # [{preference_id, matched, severity, evidence_snippet, source_url, confidence}]
    hard_exclusion_confirmed: Mapped[bool] = mapped_column(default=False)
    # owner has seen a hard-exclusion warning and chosen to proceed anyway (section 4)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    applications: Mapped[list["Application"]] = relationship(back_populates="company")
    contacts: Mapped[list["Contact"]] = relationship(back_populates="company")


class Application(Base):
    """One application to one company (section 5 steps 3-6)."""

    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    cv_id: Mapped[int | None] = mapped_column(ForeignKey("cvs.id"), nullable=True)
    # The posting this application answers (roadmap phase 3); None = open application.
    job_posting_id: Mapped[int | None] = mapped_column(ForeignKey("job_postings.id"), nullable=True)
    # A CV assembled for this application (phase 5); attached instead of cv_id once approved.
    generated_cv_id: Mapped[int | None] = mapped_column(nullable=True)
    # How the owner applied: email | site_form | ats_form | contact_form (phase 6).
    channel: Mapped[str] = mapped_column(String, default="email")
    # When a reply / interview was recorded (phase 7 funnel: time to reply).
    replied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Final outcome + an archive of what was sent: {result, note, at, archive{...}}
    outcome: Mapped[dict] = mapped_column(JSON, default=dict)
    target_position: Mapped[str] = mapped_column(String, default="")
    # Owner-entered reason for reaching out (open/speculative applications have
    # no job posting - the reason is what the draft is grounded in instead).
    application_reason: Mapped[str] = mapped_column(Text, default="")
    subject: Mapped[str] = mapped_column(String, default="")
    cover_letter_body: Mapped[str] = mapped_column(Text, default="")
    sources: Mapped[list] = mapped_column(JSON, default=list)  # source URLs backing claims (Evidence Rule)
    # Owner-side evidence: the ProfileItems retrieved for this draft, and which
    # of them the letter drew on (app/applications/evidence.py).
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    # [{item_id, source_type, source_ref, source_url, purpose, why, used}]
    excluded_evidence_ids: Mapped[list] = mapped_column(JSON, default=list)  # owner: "don't use in this draft"
    state: Mapped[ApplicationState] = mapped_column(Enum(ApplicationState), default=ApplicationState.DRAFT)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_action_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
    # When the reply check last ran for this application (manual or automatic).
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # When the next follow-up is due (sent_at + NO_RESPONSE_DAYS, pushed forward
    # each time a follow-up is sent). Drives the dashboard "no reply yet" queue.
    follow_up_due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    gmail_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    gmail_thread_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Our own RFC 2822 Message-ID, set before sending, so a send that timed out
    # after Gmail accepted it can be found in the Sent folder (never resent).
    rfc_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    sent_to: Mapped[str | None] = mapped_column(String, nullable=True)
    # The exact file attached at send time: {path, sha256, name}. `path` is a
    # copy under data/sent_cvs/, so replacing a CV variant can't rewrite history.
    cv_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    # Things the reply check saw that aren't replies: auto-replies, bounces,
    # possible replies outside the thread. [{kind, detail, message_id, at, ...}]
    notes: Mapped[list] = mapped_column(JSON, default=list)
    revision_history: Mapped[list] = mapped_column(JSON, default=list)  # [{feedback, previous_body, at}]

    company: Mapped["Company"] = relationship(back_populates="applications")
    cv: Mapped["CV | None"] = relationship()
    job_posting: Mapped["JobPosting | None"] = relationship()
    follow_ups: Mapped[list["FollowUp"]] = relationship(back_populates="application")


class FollowUp(Base):
    """A short reminder email sent in the SAME Gmail thread as its application
    when no reply arrived. Human-approved like everything else - never auto-sent."""

    __tablename__ = "follow_ups"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"))
    body: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[FollowUpState] = mapped_column(Enum(FollowUpState), default=FollowUpState.PENDING_APPROVAL)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    gmail_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    application: Mapped["Application"] = relationship(back_populates="follow_ups")


class Contact(Base):
    """Employee fallback contact (section 5 step 7). Manually entered in v1."""

    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    name: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String, default="")
    email: Mapped[str] = mapped_column(String)
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    added_manually: Mapped[bool] = mapped_column(default=True)
    state: Mapped[ContactState] = mapped_column(Enum(ContactState), default=ContactState.NOT_CONTACTED)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"), nullable=True)

    company: Mapped["Company"] = relationship(back_populates="contacts")


class DiscoverySource(Base):
    """A technopark company directory the owner imports candidates from."""

    __tablename__ = "discovery_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)  # the technopark, e.g. "ODTÜ Teknokent"
    city: Mapped[str] = mapped_column(String, default="")
    url: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String, default="html")  # "odtu_json" | "html" | "json" | "file" | "github_orgs" | ...
    # Reader settings: field mapping for "json"/"file" sources, location for "github_orgs".
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    last_imported_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")


class CompanyCandidate(Base):
    """A company found in a directory. Becomes a Company only on owner approval."""

    __tablename__ = "company_candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("discovery_sources.id"))
    name: Mapped[str] = mapped_column(String)  # as listed - often the legal name
    website_url: Mapped[str | None] = mapped_column(String, nullable=True)  # only if the directory lists one
    sector: Mapped[str] = mapped_column(String, default="")  # the directory's own category, if it has one
    contact_email: Mapped[str | None] = mapped_column(String, nullable=True)  # as listed in the directory
    city: Mapped[str] = mapped_column(String, default="")
    technopark: Mapped[str] = mapped_column(String, default="")
    status: Mapped[CandidateStatus] = mapped_column(Enum(CandidateStatus), default=CandidateStatus.PENDING)
    # Homepage screening: {brand_name, summary, sector, fit, fit_reason,
    # careers_page, flags[{preference_id, type, description, evidence_snippet}], error}
    triage: Mapped[dict] = mapped_column(JSON, default=dict)
    triaged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # No-LLM liveness check (app/discovery/liveness.py): {status, signals[], checked_at, final_url}
    liveness: Mapped[dict] = mapped_column(JSON, default=dict)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    source: Mapped["DiscoverySource"] = relationship()


class LLMCallLog(Base):
    """One row per LLM call (CLAUDE.md section 8): what it was for, which model,
    how many tokens, and which company / application / profile item it served.
    Written by app/llm/call_log.py around every provider; read by /costs."""

    __tablename__ = "llm_call_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    purpose: Mapped[str] = mapped_column(String, default="unknown")
    provider: Mapped[str] = mapped_column(String, default="")
    model: Mapped[str] = mapped_column(String, default="")
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    company_id: Mapped[int | None] = mapped_column(nullable=True)
    application_id: Mapped[int | None] = mapped_column(nullable=True)
    profile_item_id: Mapped[int | None] = mapped_column(nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class PostingStatus(str, enum.Enum):
    NEW = "NEW"
    SHORTLISTED = "SHORTLISTED"
    APPLIED = "APPLIED"
    EXPIRED = "EXPIRED"
    DISMISSED = "DISMISSED"


class JobPosting(Base):
    """A specific job posting (roadmap phase 3, posting-first entry point).
    Enters by owner paste, a careers-page change, or a public ATS board."""

    __tablename__ = "job_postings"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    source: Mapped[str] = mapped_column(String, default="manual")  # manual | careers_page | ats_api | linkedin
    url: Mapped[str] = mapped_column(String, default="")
    apply_url: Mapped[str] = mapped_column(String, default="")
    title: Mapped[str] = mapped_column(String, default="")
    company_name: Mapped[str] = mapped_column(String, default="")  # as the posting names it
    location: Mapped[str] = mapped_column(String, default="")
    work_mode: Mapped[str] = mapped_column(String, default="unknown")  # onsite | hybrid | remote | unknown
    language: Mapped[str] = mapped_column(String, default="")  # tr | en
    seniority: Mapped[str] = mapped_column(String, default="unknown")
    description_text: Mapped[str] = mapped_column(Text, default="")  # sanitized, full
    requirements: Mapped[list] = mapped_column(JSON, default=list)
    nice_to_have: Mapped[list] = mapped_column(JSON, default=list)
    deadline: Mapped[str] = mapped_column(String, default="")
    date_posted: Mapped[str] = mapped_column(String, default="")
    content_hash: Mapped[str] = mapped_column(String, default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    status: Mapped[PostingStatus] = mapped_column(Enum(PostingStatus), default=PostingStatus.NEW)
    # Found automatically (ATS board, careers watch) and not yet looked at:
    # waits on its own list until the owner keeps or dismisses it.
    inbox: Mapped[bool] = mapped_column(Boolean, default=False)
    # Deterministic gates: {location: {result, quote}, language, seniority, remote_eligibility}
    gates: Mapped[dict] = mapped_column(JSON, default=dict)
    # Fit evaluation: {verdict, scores{dim: {score, posting_quote, evidence_ids, note}}, strengths[], gaps[]}
    fit: Mapped[dict] = mapped_column(JSON, default=dict)
    legitimacy_flags: Mapped[list] = mapped_column(JSON, default=list)
    preference_check: Mapped[list] = mapped_column(JSON, default=list)
    hard_exclusion_confirmed: Mapped[bool] = mapped_column(default=False)
    # Unmatched company the owner may approve: {name, website_url}
    proposed_company: Mapped[dict] = mapped_column(JSON, default=dict)
    content_safety: Mapped[dict] = mapped_column(JSON, default=dict)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)  # ATS ids, parse notes, expiry check result
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    company: Mapped["Company | None"] = relationship()


class AtsBoard(Base):
    """A company's public ATS job board (Greenhouse, Lever, Ashby,
    SmartRecruiters ...), found on its site or via the search API. Postings
    are always read fresh from the board's public endpoint."""

    __tablename__ = "ats_boards"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String)
    token: Mapped[str] = mapped_column(String)
    url: Mapped[str] = mapped_column(String, default="")
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    company_name: Mapped[str] = mapped_column(String, default="")
    found_via: Mapped[str] = mapped_column(String, default="site")  # site | search | manual
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_result: Mapped[dict] = mapped_column(JSON, default=dict)  # {accepted, ambiguous, rejected, error}
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class StyleExample(Base):
    """An approved letter the owner marked as a good example (roadmap phase 4).
    The two most similar ones (same language, same CV target role) are shown
    to the writer as style references."""

    __tablename__ = "style_examples"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"), nullable=True)
    language: Mapped[str] = mapped_column(String, default="")
    target_role: Mapped[str] = mapped_column(String, default="")
    subject: Mapped[str] = mapped_column(String, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class StyleNote(Base):
    """A standing writing rule the owner promoted from feedback ("never
    mention GPA"). Active notes go into every drafting prompt."""

    __tablename__ = "style_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(default=True)
    source_application_id: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class ProfileFactStatus(str, enum.Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


class ProfileFact(Base):
    """A fact about the owner stated outside ingestion - in reject feedback or
    a draft edit (roadmap phase 4, fact capture). Proposed by the app, added to
    the profile only on the owner's approval (as a ProfileItem with
    source_type "owner", so it counts as evidence)."""

    __tablename__ = "profile_facts"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    quote: Mapped[str] = mapped_column(Text, default="")  # the owner's own words it came from
    source_kind: Mapped[str] = mapped_column(String, default="feedback")  # feedback | edit
    application_id: Mapped[int | None] = mapped_column(nullable=True)
    status: Mapped[ProfileFactStatus] = mapped_column(Enum(ProfileFactStatus), default=ProfileFactStatus.PROPOSED)
    profile_item_id: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class CVProject(Base):
    """A project that may appear on generated CVs (roadmap phase 5). The app
    suggests them from profile records; the owner's select/deselect always wins."""

    __tablename__ = "cv_projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_item_id: Mapped[int | None] = mapped_column(ForeignKey("profile_items.id"), nullable=True)
    name: Mapped[str] = mapped_column(String)
    subtitle: Mapped[str] = mapped_column(String, default="")  # e.g. "Graduation Project"
    link_url: Mapped[str] = mapped_column(String, default="")
    selected: Mapped[bool] = mapped_column(default=True)
    owner_decided: Mapped[bool] = mapped_column(default=False)  # suggestions never flip an owner choice
    # high | normal | low - how many bullets the project gets (3 / 2 / 1).
    priority: Mapped[str] = mapped_column(String, default="normal")
    score: Mapped[int] = mapped_column(default=0)
    sort_order: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    profile_item: Mapped["ProfileItem | None"] = relationship()
    bullets: Mapped[list["Bullet"]] = relationship(back_populates="project", order_by="Bullet.sort_order")


class Bullet(Base):
    """One CV bullet in the bank: written once per project, approved by the
    owner, then reused by every generated CV. Edited bullets are locked - new
    suggestions wait in `pending_text`, never overwriting the owner's words."""

    __tablename__ = "bullets"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("cv_projects.id"))
    text: Mapped[str] = mapped_column(Text)
    tags: Mapped[dict] = mapped_column(JSON, default=dict)  # {tech[], domain[], role_types[]}
    source: Mapped[dict] = mapped_column(JSON, default=dict)  # {kind: result|owner_fact|record, quote}
    status: Mapped[str] = mapped_column(String, default="draft")  # draft | approved
    locked: Mapped[bool] = mapped_column(default=False)
    pending_text: Mapped[str] = mapped_column(Text, default="")
    flags: Mapped[dict] = mapped_column(JSON, default=dict)  # {ungrounded_numbers[], ungrounded_tech[], needs_metric}
    sort_order: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    project: Mapped["CVProject"] = relationship(back_populates="bullets")


class TechSkill(Base):
    """One technology on the CV's Technical Skills list, with where it is used."""

    __tablename__ = "tech_skills"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    group: Mapped[str] = mapped_column(String, default="frameworks")  # languages | frameworks | technologies | genai
    # [{kind: repo|experience|manual, ref, item_id}]
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    manual: Mapped[bool] = mapped_column(default=False)
    hidden: Mapped[bool] = mapped_column(default=False)
    # core: on every CV. specific: a niche tool, only on a CV whose posting asks for it.
    tier: Mapped[str] = mapped_column(String, default="core")
    tier_set_by_owner: Mapped[bool] = mapped_column(default=False)
    sort_order: Mapped[int] = mapped_column(default=0)


class GeneratedCV(Base):
    """A CV assembled for one application (roadmap phase 5): what was chosen,
    the rendered PDF and its checks. Approved -> attached to the application."""

    __tablename__ = "generated_cvs"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"), nullable=True)
    job_posting_id: Mapped[int | None] = mapped_column(ForeignKey("job_postings.id"), nullable=True)
    # {project_ids[], bullet_ids{project_id: [ids]}, bullet_overrides{id: text}, skills{group: [names]},
    #  certificates: bool, role_type, keywords[]}
    selection: Mapped[dict] = mapped_column(JSON, default=dict)
    language: Mapped[str] = mapped_column(String, default="en")
    pdf_path: Mapped[str] = mapped_column(String, default="")
    sha256: Mapped[str] = mapped_column(String, default="")
    keyword_report: Mapped[dict] = mapped_column(JSON, default=dict)  # {covered[], missing[]}
    ats_report: Mapped[dict] = mapped_column(JSON, default=dict)  # {passed, checks[], page_count, word_diffs[]}
    status: Mapped[str] = mapped_column(String, default="draft")  # draft | approved
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
