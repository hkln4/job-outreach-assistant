from app.db import SessionLocal, init_db
from app.models import Company, CompanyState, Preference, PreferenceType


def setup_module():
    init_db()


def test_company_defaults_to_applicable():
    db = SessionLocal()
    try:
        company = Company(name="Test Co", website_url="https://example.com")
        db.add(company)
        db.commit()
        db.refresh(company)

        assert company.state == CompanyState.APPLICABLE
        assert company.research == {}
        assert company.preference_check == []
    finally:
        db.close()


def test_preference_round_trip():
    db = SessionLocal()
    try:
        pref = Preference(description="Avoid defense industry", type=PreferenceType.HARD, signals=["defense", "military"])
        db.add(pref)
        db.commit()
        db.refresh(pref)

        fetched = db.get(Preference, pref.id)
        assert fetched.type == PreferenceType.HARD
        assert "defense" in fetched.signals
    finally:
        db.close()
