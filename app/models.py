"""
Entities and state machines, per CLAUDE.md sections 6 and 7.

JSON-shaped fields (skills[], source_urls[], research, preference_check[], etc.)
are stored as SQLite JSON columns rather than normalized child tables - this is a
single-user app and those fields are always read/written as a whole per item.
"""
import enum
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, String, Text
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Preference(Base):
    """Owner constraint checked against every company during research (section 4)."""

    __tablename__ = "preferences"

    id: Mapped[int] = mapped_column(primary_key=True)
    description: Mapped[str] = mapped_column(Text)
    type: Mapped[PreferenceType] = mapped_column(Enum(PreferenceType))
    signals: Mapped[list] = mapped_column(JSON, default=list)  # optional keywords/notes


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
    revision_history: Mapped[list] = mapped_column(JSON, default=list)  # [{feedback, previous_body, at}]

    company: Mapped["Company"] = relationship(back_populates="applications")
    cv: Mapped["CV | None"] = relationship()
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
    kind: Mapped[str] = mapped_column(String, default="html")  # "odtu_json" | "html"
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
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    source: Mapped["DiscoverySource"] = relationship()
