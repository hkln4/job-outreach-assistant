"""Phase 1 item 6: a CV becomes one record per entry, bullets verbatim, and
re-ingesting keeps the owner's edits."""
import json

from app.db import SessionLocal, init_db
from app.models import ProfileItem, ProfileItemStatus
from app.profile_builder.entries import ingest_document_entries
from tests.fakes import FakeLLMProvider

CV_TEXT = """Hasan Kalın
PROFESSIONAL EXPERIENCE
Data Science Intern, Acme Analytics  Jun 2024 - Sep 2024
• Built a mid-
LTV churn model with LightGBM on BigQuery data
• Cut feature pipeline runtime by 40%
EDUCATION
B.Sc. Computer Engineering, Hacettepe University 2020 - 2025
"""


def _answer(*entries) -> str:
    return json.dumps({"entries": list(entries)})


def _entry(entry_type, title, organization, bullets, stack=()):
    return {"entry_type": entry_type, "title": title, "organization": organization, "start": "", "end": "",
            "location": "", "bullets": list(bullets), "stack": list(stack), "purpose": "x"}


INTERN = _entry(
    "experience", "Data Science Intern", "Acme Analytics",
    ["Built a mid-LTV churn model with LightGBM on BigQuery data", "Cut feature pipeline runtime by 40%", "Led a team of 10"],
    ["LightGBM", "BigQuery", "Kubernetes"],
)
EDU = _entry("education", "B.Sc. Computer Engineering", "Hacettepe University", [])


def setup_module():
    init_db()


def test_cv_splits_into_verified_entries_and_reingest_keeps_owner_edits():
    path = "/tmp/owner-cv-entries.pdf"
    db = SessionLocal()
    try:
        intern, edu = ingest_document_entries(db, FakeLLMProvider(_answer(INTERN, EDU)), path, CV_TEXT, None, "cv")
        assert intern.parent_ref == path and edu.parent_ref == path
        assert intern.source_ref == f"{path}#experience:data-science-intern@acme-analytics"
        # the PDF broke "mid-LTV" across a line; still verbatim. "Led a team of 10" isn't in the CV.
        assert intern.extracted["bullets"] == INTERN["bullets"][:2]
        assert intern.extracted["unverified_bullets"] == ["Led a team of 10"]
        assert intern.extracted["stack"] == ["LightGBM", "BigQuery"]
        assert intern.extracted["role"] == "Data Science Intern @ Acme Analytics"

        intern.owner_edited_fields = ["purpose"]
        intern.extracted = {**intern.extracted, "purpose": "My internship"}
        db.commit()

        # re-ingest: same refs, owner edit survives; education vanished and had no owner input -> deleted
        (again,) = ingest_document_entries(db, FakeLLMProvider(_answer(INTERN)), path, CV_TEXT, None, "cv")
        assert again.id == intern.id and again.extracted["purpose"] == "My internship"
        assert db.query(ProfileItem).filter(ProfileItem.parent_ref == path).count() == 1
    finally:
        db.query(ProfileItem).filter(ProfileItem.parent_ref == path).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_failed_split_is_one_needs_review_record_for_the_file():
    db = SessionLocal()
    try:
        (item,) = ingest_document_entries(db, FakeLLMProvider("not json"), "/tmp/broken-cv.pdf", "text", None, "cv")
        assert item.source_ref == "/tmp/broken-cv.pdf" and item.status == ProfileItemStatus.NEEDS_REVIEW
    finally:
        db.close()
