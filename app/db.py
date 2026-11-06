"""
SQLAlchemy engine/session setup. Single-user SQLite (section 12).
"""
from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

# check_same_thread=False: the review UI is single-process local-only, but
# uvicorn may serve requests from a worker thread pool.
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: yields a session, closes it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Columns added to existing tables after the initial release. create_all() only
# creates missing TABLES, never adds columns to a table that already exists -
# so a plain new mapped_column() on an existing model silently does nothing on
# a database that already has that table. Each entry here is applied with a
# non-destructive ALTER TABLE ADD COLUMN if missing - existing rows/data are
# never touched or rebuilt.
_COLUMN_MIGRATIONS = [
    ("profiles", "cover_letter_template", "TEXT DEFAULT ''"),
    ("companies", "why_interested", "TEXT DEFAULT ''"),
    ("companies", "location", "VARCHAR DEFAULT ''"),
    ("companies", "technopark", "VARCHAR DEFAULT ''"),
    ("companies", "linkedin_url", "VARCHAR"),
    ("companies", "linkedin_research", "JSON DEFAULT '{}'"),
    ("applications", "application_reason", "TEXT DEFAULT ''"),
    ("applications", "last_checked_at", "DATETIME"),
    ("applications", "follow_up_due_at", "DATETIME"),
    ("applications", "evidence", "JSON DEFAULT '[]'"),
    ("applications", "excluded_evidence_ids", "JSON DEFAULT '[]'"),
    ("company_candidates", "sector", "VARCHAR DEFAULT ''"),
    ("company_candidates", "contact_email", "VARCHAR"),
    ("companies", "invalid_emails", "JSON DEFAULT '[]'"),
    ("applications", "rfc_message_id", "VARCHAR"),
    ("applications", "sent_to", "VARCHAR"),
    ("applications", "cv_snapshot", "JSON DEFAULT '{}'"),
    ("applications", "notes", "JSON DEFAULT '[]'"),
    ("profile_items", "facts", "JSON DEFAULT '{}'"),
    ("profile_items", "source_version", "JSON DEFAULT '{}'"),
    ("profile_items", "owner_facts", "JSON DEFAULT '[]'"),
    ("profile_items", "owner_edited_fields", "JSON DEFAULT '[]'"),
    ("profile_items", "pending_update", "JSON DEFAULT '{}'"),
    ("profile_items", "parent_ref", "VARCHAR"),
    ("profiles", "writeup", "TEXT DEFAULT ''"),
    ("profiles", "writeup_generated_at", "DATETIME"),
    ("profiles", "writeup_unknown_tags", "JSON DEFAULT '[]'"),
    ("company_candidates", "liveness", "JSON DEFAULT '{}'"),
    ("companies", "liveness", "JSON DEFAULT '{}'"),
    ("discovery_sources", "config", "JSON DEFAULT '{}'"),
    ("applications", "job_posting_id", "INTEGER REFERENCES job_postings(id)"),
    ("applications", "channel", "VARCHAR DEFAULT 'email'"),
    *[("owner_settings", column, "VARCHAR DEFAULT ''") for column in (
        "full_name", "title", "email", "phone", "city", "linkedin_url", "github_url", "portfolio_url")],
    ("owner_settings", "base_cv_id", "INTEGER"),
    ("owner_settings", "cv_template_approved", "BOOLEAN DEFAULT 0"),
    ("applications", "generated_cv_id", "INTEGER"),
    ("applications", "replied_at", "DATETIME"),
    ("applications", "outcome", "JSON DEFAULT '{}'"),
    ("owner_settings", "excluded_repos", "JSON DEFAULT '[]'"),
    ("cv_projects", "priority", "VARCHAR DEFAULT 'normal'"),
    ("tech_skills", "tier", "VARCHAR DEFAULT 'core'"),
    ("tech_skills", "tier_set_by_owner", "BOOLEAN DEFAULT 0"),
]


def _apply_column_migrations() -> None:
    inspector = inspect(engine)
    if "profiles" not in inspector.get_table_names():
        return  # fresh database - create_all() above already created the up-to-date shape
    with engine.begin() as conn:
        for table, column, ddl_type in _COLUMN_MIGRATIONS:
            existing = {col["name"] for col in inspector.get_columns(table)}
            if column not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))


def init_db() -> None:
    """Create all tables that don't exist yet. Called on app startup / by scripts/init_db.py."""
    from app import models  # noqa: F401  (ensure models are registered on Base.metadata)

    Base.metadata.create_all(bind=engine)
    _apply_column_migrations()
