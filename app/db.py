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
    ("applications", "application_reason", "TEXT DEFAULT ''"),
    ("applications", "last_checked_at", "DATETIME"),
    ("applications", "follow_up_due_at", "DATETIME"),
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
