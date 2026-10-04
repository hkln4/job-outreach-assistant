import json

import app.companies.workflow as workflow
from app.db import SessionLocal, init_db
from app.models import Company, Preference, PreferenceType
from tests.fakes import SequencedLLMProvider

# Fixtures need enough text to read as a real page (see MIN_USABLE_RESEARCH_CHARS).
PAGE_PAD = " The company works with logistics and manufacturing customers across Turkey, runs its own engineering team in Ankara, and publishes case studies about the systems it has delivered over the past several years."


def setup_module():
    init_db()


def test_research_company_caches_pages_and_flags_hard_exclusion(monkeypatch):
    def fake_fetch(website_url, careers_url=None):
        return {
            "pages": [{"url": website_url, "text": "We are proud partners of the defense sector." + PAGE_PAD}],
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


def test_classify_email_hr_vs_general():
    assert workflow.classify_email("hr@acme.example") == "hr"
    assert workflow.classify_email("ik@acme.example") == "hr"
    assert workflow.classify_email("insan.kaynaklari@acme.example") == "hr"
    assert workflow.classify_email("kariyer@acme.example") == "hr"
    assert workflow.classify_email("recruiting@acme.example") == "hr"
    assert workflow.classify_email("info@acme.example") == "general"
    assert workflow.classify_email("sales@acme.example") == "general"
    # "ik" must not match as a substring of an unrelated word
    assert workflow.classify_email("teknik@acme.example") == "general"
    assert workflow.classify_email("iletisim@acme.example") == "general"


def test_pick_contact_email_prefers_hr():
    emails = [
        {"email": "info@acme.example", "context": "general", "kind": "general"},
        {"email": "not-an-email", "context": "", "kind": "general"},
        {"email": "hr@acme.example", "context": "HR", "kind": "hr"},
    ]
    assert workflow.pick_contact_email(emails)["email"] == "hr@acme.example"
    # no HR address -> first valid one
    assert workflow.pick_contact_email(emails[:2])["email"] == "info@acme.example"
    assert workflow.pick_contact_email([]) is None


def test_research_stores_positions_careers_emails_and_fit(monkeypatch):
    def fake_fetch(website_url, careers_url=None):
        return {
            "pages": [{"url": website_url, "text": "We build CV pipelines. Careers: ML Engineer. ik@visionco.example info@visionco.example" + PAGE_PAD}],
            "detected_careers_url": None,
        }

    monkeypatch.setattr(workflow, "fetch_company_research", fake_fetch)

    db = SessionLocal()
    try:
        company = Company(name="VisionCo", website_url="https://visionco.example")
        db.add(company)
        db.commit()
        db.refresh(company)

        llm = SequencedLLMProvider(
            [
                json.dumps(
                    {
                        "summary": "Computer vision company building CV pipelines.",
                        "open_positions": [{"title": "ML Engineer", "details": "Python, PyTorch"}],
                        "careers": {
                            "has_careers_page": True,
                            "application_form": True,
                            "external_portal": "kariyer.net",
                            "notes": "",
                        },
                        "channels": ["form", "kariyer.net"],
                        "emails": [
                            {"email": "info@visionco.example", "context": "general info"},
                            {"email": "ik@visionco.example", "context": "HR contact"},
                        ],
                        "people": [],
                        "fit": {"recommended_position": "ML Engineer", "reasoning": "Matches CV/ML target roles."},
                    }
                ),  # research call
                # no preference-check call: DB may hold prefs from the other test,
                # so provide a no-flags response just in case
                json.dumps({"flags": []}),
            ]
        )

        result = workflow.research_company(db, llm, company)

        assert result.research["open_positions"][0]["title"] == "ML Engineer"
        assert result.research["careers"]["application_form"] is True
        assert result.research["careers"]["external_portal"] == "kariyer.net"
        assert result.research["fit"]["recommended_position"] == "ML Engineer"
        # HR address wins over the general one even though it was listed second
        assert result.contact_email == "ik@visionco.example"
        assert [e["kind"] for e in result.research["emails"]] == ["general", "hr"]
    finally:
        db.close()


def test_research_backfills_linkedin_url_but_never_overwrites_the_owners(monkeypatch):
    def fake_fetch(website_url, careers_url=None):
        return {
            "pages": [{"url": website_url, "text": "We build widgets." + PAGE_PAD}],
            "detected_careers_url": None,
            "detected_linkedin_url": "https://www.linkedin.com/company/detected",
        }

    monkeypatch.setattr(workflow, "fetch_company_research", fake_fetch)

    db = SessionLocal()
    try:
        found = Company(name="Widget Co", website_url="https://widget.example")
        owner_set = Company(
            name="Widget Two",
            website_url="https://widget2.example",
            linkedin_url="https://www.linkedin.com/company/owner-typed",
        )
        db.add_all([found, owner_set])
        db.commit()

        for company in (found, owner_set):
            workflow.research_company(db, SequencedLLMProvider(["{}", "{}"]), company)

        assert found.linkedin_url == "https://www.linkedin.com/company/detected"
        assert owner_set.linkedin_url == "https://www.linkedin.com/company/owner-typed"
    finally:
        db.close()


def test_empty_page_text_is_recorded_as_empty_not_as_research(monkeypatch):
    """A fetch can succeed and still yield nothing (JS-only site, empty shell).
    Storing that as normal research is what let drafting run on a blank page."""
    def fake_fetch(website_url, careers_url=None):
        return {"pages": [{"url": website_url, "text": ""}], "detected_careers_url": None}

    monkeypatch.setattr(workflow, "fetch_company_research", fake_fetch)

    db = SessionLocal()
    try:
        company = Company(name="Blank Shell Co", website_url="https://blank.example")
        db.add(company)
        db.commit()

        llm = SequencedLLMProvider(["{}", "{}"])
        workflow.research_company(db, llm, company)

        assert company.research["empty"] is True
        assert company.research["content_chars"] == 0
        assert workflow.has_usable_research(company) is False
        # no LLM call is worth making against a blank page
        assert llm.calls == []
    finally:
        db.close()


def test_has_usable_research_covers_records_written_before_the_check():
    db = SessionLocal()
    try:
        old_empty = Company(name="Old Empty Co", research={"pages": [{"url": "x", "text": ""}]})
        old_good = Company(name="Old Good Co", research={"pages": [{"url": "x", "text": "y" * 500}]})
        failed = Company(name="Failed Co", research={"pages": [], "error": "boom"})
        never = Company(name="Never Co")
        db.add_all([old_empty, old_good, failed, never])
        db.commit()

        assert workflow.has_usable_research(old_empty) is False
        assert workflow.has_usable_research(old_good) is True
        assert workflow.has_usable_research(failed) is False
        assert workflow.has_usable_research(never) is False
    finally:
        db.close()
