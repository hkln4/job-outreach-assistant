"""Roadmap phase 7: outcomes archive what was sent; the funnel groups sent
applications with reply / interview rates and the median time to reply."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.applications.outcomes import funnel, record_outcome
from app.db import SessionLocal, init_db
from app.main import app
from app.models import CV, Application, ApplicationState, Company, JobPosting
from tests.conftest import make_cv_file


def test_outcome_archive_and_funnel_rates():
    init_db()
    db = SessionLocal()
    try:
        if db.query(CV).first() is None:
            db.add(CV(name="Funnel Gate CV", file_path=make_cv_file("funnel-gate.pdf")))
        company = Company(name="Funnel Co", technopark="ODTÜ Teknokent")
        db.add(company)
        db.commit()
        posting = JobPosting(title="ML Engineer", url="https://funnel.example/job", description_text="Posting text", source="ats_api")
        db.add(posting)
        db.commit()
        now = datetime.now(timezone.utc)
        a = Application(company_id=company.id, job_posting_id=posting.id, subject="S", cover_letter_body="Hello and thanks for the role",
                        state=ApplicationState.AWAITING_REPLY, sent_at=now - timedelta(days=6), channel="email")
        b = Application(company_id=company.id, subject="S", cover_letter_body="Merhaba, başvurum için teşekkürler",
                        state=ApplicationState.AWAITING_REPLY, sent_at=now - timedelta(days=3), channel="site_form")
        db.add_all([a, b])
        db.commit()
        record_outcome(db, a, ApplicationState.INTERVIEW, "first round on Monday")
        assert a.outcome["archive"]["posting"]["text"] == "Posting text" and a.outcome["note"] == "first round on Monday"
        assert a.replied_at is not None

        data = funnel(db)
        channels = {row["key"]: row for row in data["by"]["channel"]}
        assert channels["email"]["interviews"] >= 1 and channels["site_form"]["replies"] == 0
        sources = {row["key"] for row in data["by"]["source"]}
        assert "posting: ats_api" in sources and "directory: ODTÜ Teknokent" in sources
        assert channels["email"]["median_days_to_reply"] is not None
    finally:
        db.close()
    assert TestClient(app).get("/funnel").status_code == 200
