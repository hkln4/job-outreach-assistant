import json

from docx import Document

from app.db import SessionLocal, init_db
from app.models import ProfileItemStatus
from app.profile_builder.coverage import ledger_summary, record_item
from app.profile_builder.cv_parser import extract_text, ingest_cv
from tests.fakes import FakeLLMProvider


def setup_module():
    init_db()


def test_extract_text_from_docx(tmp_path):
    path = tmp_path / "cv.docx"
    doc = Document()
    doc.add_paragraph("Hasan Kalin")
    doc.add_paragraph("Backend engineer with Python and SQL experience.")
    doc.save(path)

    text = extract_text(str(path))
    assert "Hasan Kalin" in text
    assert "Python" in text


def test_ingest_cv_records_analyzed_item(tmp_path):
    path = tmp_path / "cv.docx"
    doc = Document()
    doc.add_paragraph("Backend engineer with Python experience.")
    doc.save(path)

    llm = FakeLLMProvider(
        json.dumps(
            {
                "purpose": "Professional CV",
                "stack": ["Python", "SQL"],
                "role": "Backend Engineer",
                "highlights": ["5 years experience"],
            }
        )
    )
    db = SessionLocal()
    try:
        item = ingest_cv(db, llm, str(path))
        assert item.status == ProfileItemStatus.ANALYZED
        assert item.extracted["stack"] == ["Python", "SQL"]
        assert len(llm.calls) == 1
    finally:
        db.close()


def test_ingest_cv_flags_needs_review_on_malformed_llm_output(tmp_path):
    path = tmp_path / "cv.docx"
    doc = Document()
    doc.add_paragraph("Some text")
    doc.save(path)

    llm = FakeLLMProvider("not valid json")
    db = SessionLocal()
    try:
        item = ingest_cv(db, llm, str(path))
        assert item.status == ProfileItemStatus.NEEDS_REVIEW
    finally:
        db.close()


def test_ingest_cv_survives_corrupt_pdf_instead_of_crashing(tmp_path):
    """Regression: a corrupt/unreadable file must become a needs_review item,
    not an unhandled exception that 500s the request."""
    path = tmp_path / "corrupt.pdf"
    path.write_bytes(b"not a real pdf at all")

    llm = FakeLLMProvider("should never be called")
    db = SessionLocal()
    try:
        item = ingest_cv(db, llm, str(path))
        assert item.status == ProfileItemStatus.NEEDS_REVIEW
        assert "parse_error" in item.extracted
        assert len(llm.calls) == 0  # no point calling the LLM with no text
    finally:
        db.close()


def test_ledger_summary_counts_by_source_type_and_status():
    db = SessionLocal()
    try:
        record_item(
            db,
            source_type="cv",
            source_ref="a.pdf",
            extracted={"purpose": "x", "stack": ["Python"], "role": "y", "highlights": []},
            source_urls=[],
        )
        record_item(
            db, source_type="cv", source_ref="b.pdf", extracted={"parse_error": "bad"}, source_urls=[]
        )
        summary = ledger_summary(db)
        assert summary["cv"]["analyzed"] >= 1
        assert summary["cv"]["needs_review"] >= 1
    finally:
        db.close()
