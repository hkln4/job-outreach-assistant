import json

from docx import Document
from fastapi.testclient import TestClient

from app.db import SessionLocal, init_db
from app.main import app
from app.models import CV, ProfileItem, ProfileItemStatus
from app.web.deps import get_llm
from tests.fakes import FakeLLMProvider


def setup_module():
    init_db()


def teardown_module():
    app.dependency_overrides.pop(get_llm, None)


def test_retry_reingests_a_failed_item_and_removes_the_old_one(tmp_path):
    # seed a CV so the onboarding gate lets requests through, and a CV file to retry against
    cv_path = tmp_path / "cv.docx"
    doc = Document()
    doc.add_paragraph("Backend engineer with Python experience.")
    doc.save(cv_path)

    db = SessionLocal()
    try:
        if db.query(CV).first() is None:
            db.add(CV(name="Retry Test CV", file_path=str(cv_path)))
            db.commit()

        failed_item = ProfileItem(
            source_type="cv",
            source_ref=str(cv_path),
            extracted={"purpose": "unknown", "stack": [], "role": "unknown", "highlights": [], "parse_error": "quota exceeded"},
            status=ProfileItemStatus.NEEDS_REVIEW,
            source_urls=[],
        )
        db.add(failed_item)
        db.commit()
        db.refresh(failed_item)
        item_id = failed_item.id
    finally:
        db.close()

    fake_llm = FakeLLMProvider(
        json.dumps(
            {
                "purpose": "Backend engineer CV",
                "stack": ["Python"],
                "role": "Backend Engineer",
                "highlights": ["Real extraction this time"],
            }
        )
    )
    app.dependency_overrides[get_llm] = lambda: fake_llm

    client = TestClient(app)
    r = client.post(f"/profile/items/{item_id}/retry", follow_redirects=True)
    assert r.status_code == 200

    db = SessionLocal()
    try:
        # exactly one row for this source now, and it's the successful re-ingest -
        # NOT asserting the old id is gone: SQLite reuses rowids once a table
        # empties, so a fresh row can legitimately land back on the same id.
        current_items = db.query(ProfileItem).filter(ProfileItem.source_ref == str(cv_path)).all()
        assert len(current_items) == 1
        assert current_items[0].status == ProfileItemStatus.ANALYZED
        assert current_items[0].extracted["stack"] == ["Python"]
        assert "parse_error" not in current_items[0].extracted
    finally:
        db.close()
