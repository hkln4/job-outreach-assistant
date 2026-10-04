"""LinkedIn research orchestration: what gets cached on the company, the
evidence guard on people, and how failures are recorded rather than raised."""
import json

import app.companies.linkedin_workflow as linkedin_workflow
from app.companies.linkedin_fetch import LinkedInFetchError
from app.db import SessionLocal, init_db
from app.models import Company
from tests.fakes import FakeLLMProvider


def setup_module():
    init_db()


FETCHED = {
    "slug": "acme",
    "source_url": "https://www.linkedin.com/company/acme",
    "sections": {
        "about": "Acme Software. 11-50 employees. Ankara, Türkiye. Founded 2018.",
        "posts": "We shipped v2 last week",
        "jobs": "Backend Engineer - Ankara",
        "employees": "Ada Lovelace - CTO\nGrace Hopper - Backend Engineer",
    },
    "people": [
        {"name": "Ada Lovelace", "profile_url": "https://www.linkedin.com/in/ada"},
        {"name": "Grace Hopper", "profile_url": "https://www.linkedin.com/in/grace"},
    ],
    "tool_errors": {},
}

LLM_ANSWER = {
    "employees_listed": "273 çalışan",
    "industry": "Software Development",
    "headquarters": "Ankara, Türkiye",
    "founded": "2018",
    "positions_summary": [
        {"group": "Engineering", "count": 12, "source": "linkedin_breakdown"},
        {"group": "Sales", "count": None, "source": "titles"},
        {"group": "", "count": 3, "source": "titles"},
    ],
    "activity": {"last_post_date": "1w", "is_active": True, "assessment": "Posts regularly."},
    "jobs": [{"title": "Backend Engineer", "location": "Ankara"}, {"title": "  ", "location": "x"}],
    "key_people": [
        {"name": "Ada Lovelace", "role": "CTO"},
        {"name": "Someone Invented", "role": "CEO"},
    ],
}


def _company(db, **kwargs) -> Company:
    company = Company(name="Acme", website_url="https://acme.example", **kwargs)
    db.add(company)
    db.commit()
    db.refresh(company)
    return company


def _fake_fetch(monkeypatch, result=FETCHED):
    def fake(linkedin_url, *, command, timeout_seconds, tool_delay_seconds, people_keywords=""):
        return result

    monkeypatch.setattr(linkedin_workflow, "fetch_linkedin_company", fake)


def test_research_caches_structured_findings(monkeypatch):
    _fake_fetch(monkeypatch)
    db = SessionLocal()
    try:
        company = _company(db, linkedin_url="https://www.linkedin.com/company/acme")
        linkedin_workflow.linkedin_research_company(db, FakeLLMProvider(json.dumps(LLM_ANSWER)), company)

        li = company.linkedin_research
        assert li["employees_listed"] == "273 çalışan"
        assert li["industry"] == "Software Development"
        assert li["source_url"] == "https://www.linkedin.com/company/acme"
        assert li["activity"] == {
            "last_post_date": "1w",
            "is_active": True,
            "assessment": "Posts regularly.",
        }
        # blank group / blank job title are dropped
        assert [g["group"] for g in li["positions_summary"]] == ["Engineering", "Sales"]
        assert li["positions_summary"][1]["count"] is None
        assert li["jobs"] == [{"title": "Backend Engineer", "location": "Ankara"}]
        assert li["fetched_at"]
        # website research is a separate lifecycle and must be left alone
        assert company.research == {}
    finally:
        db.close()


def test_people_must_appear_in_the_scraped_list(monkeypatch):
    """The model picks who is worth contacting; it may not introduce anyone."""
    _fake_fetch(monkeypatch)
    db = SessionLocal()
    try:
        company = _company(db, linkedin_url="https://www.linkedin.com/company/acme")
        linkedin_workflow.linkedin_research_company(db, FakeLLMProvider(json.dumps(LLM_ANSWER)), company)

        assert company.linkedin_research["people"] == [
            {"name": "Ada Lovelace", "role": "CTO", "profile_url": "https://www.linkedin.com/in/ada"}
        ]
    finally:
        db.close()


def test_partial_tool_failure_is_kept_visible(monkeypatch):
    partial = {**FETCHED, "sections": {"about": "Acme Software."}, "people": [], "tool_errors": {"get_company_employees": "rate limited"}}
    _fake_fetch(monkeypatch, partial)
    db = SessionLocal()
    try:
        company = _company(db, linkedin_url="https://www.linkedin.com/company/acme")
        linkedin_workflow.linkedin_research_company(db, FakeLLMProvider(json.dumps(LLM_ANSWER)), company)

        assert company.linkedin_research["tool_errors"] == {"get_company_employees": "rate limited"}
        assert company.linkedin_research["people"] == []
    finally:
        db.close()


def test_missing_linkedin_url_is_recorded_not_raised(monkeypatch):
    _fake_fetch(monkeypatch)
    db = SessionLocal()
    try:
        company = _company(db)
        linkedin_workflow.linkedin_research_company_safe(db, FakeLLMProvider("{}"), company)

        assert "no LinkedIn URL" in company.linkedin_research["error"]
    finally:
        db.close()


def test_fetch_failure_is_recorded_and_leaves_website_research_alone(monkeypatch):
    def boom(linkedin_url, **kwargs):
        raise LinkedInFetchError("session expired - run uvx mcp-server-linkedin@latest --login")

    monkeypatch.setattr(linkedin_workflow, "fetch_linkedin_company", boom)
    db = SessionLocal()
    try:
        company = _company(db, linkedin_url="https://www.linkedin.com/company/acme")
        company.research = {"pages": [{"url": "https://acme.example", "text": "hi"}]}
        db.commit()

        linkedin_workflow.linkedin_research_company_safe(db, FakeLLMProvider("{}"), company)

        assert "session expired" in company.linkedin_research["error"]
        assert company.research["pages"]
    finally:
        db.close()


def test_unparseable_llm_answer_leaves_an_empty_but_valid_card(monkeypatch):
    _fake_fetch(monkeypatch)
    db = SessionLocal()
    try:
        company = _company(db, linkedin_url="https://www.linkedin.com/company/acme")
        linkedin_workflow.linkedin_research_company(db, FakeLLMProvider("not json at all"), company)

        li = company.linkedin_research
        assert li["employees_listed"] is None
        assert li["positions_summary"] == []
        assert li["people"] == []
        assert li["fetched_at"]
    finally:
        db.close()
