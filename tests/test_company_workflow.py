import json

import app.companies.workflow as workflow
from app.db import SessionLocal, init_db
from app.models import Company, Preference, PreferenceType
from tests.fakes import SequencedLLMProvider


def setup_module():
    init_db()


def test_research_company_caches_pages_and_flags_hard_exclusion(monkeypatch):
    def fake_fetch(website_url, careers_url=None):
        return {
            "pages": [{"url": website_url, "text": "We are proud partners of the defense sector."}],
            "detected_careers_url": None,
        }

    monkeypatch.setattr(workflow, "fetch_company_research", fake_fetch)

    db = SessionLocal()
    try:
        pref = Preference(description="Avoid defense industry", type=PreferenceType.HARD, signals=["defense"])
        db.add(pref)
        db.commit()
        db.refresh(pref)

        company = Company(name="Acme Defense Co", website_url="https://acme-defense.example")
        db.add(company)
        db.commit()
        db.refresh(company)

        llm = SequencedLLMProvider(
            [
                json.dumps({"channels": ["form"]}),  # channel detection call
                json.dumps(
                    {
                        "flags": [
                            {
                                "preference_id": pref.id,
                                "evidence_snippet": "proud partners of the defense sector",
                                "confidence": "high",
                            }
                        ]
                    }
                ),  # preference check call
            ]
        )

        result = workflow.research_company(db, llm, company)

        assert result.research["pages"][0]["url"] == "https://acme-defense.example"
        assert result.other_channels == ["form"]
        assert len(result.preference_check) == 1
        assert result.preference_check[0]["severity"] == "hard"
        assert workflow.has_unconfirmed_hard_exclusion(result) is True

        result.hard_exclusion_confirmed = True
        assert workflow.has_unconfirmed_hard_exclusion(result) is False
    finally:
        db.close()
