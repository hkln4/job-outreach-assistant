"""LinkedIn research through the web layer: the whole feature stays invisible
and inert while the flag is off, and people found on LinkedIn become contacts
only after the owner adds an email."""
import json

import pytest
from fastapi.testclient import TestClient

import app.companies.linkedin_workflow as linkedin_workflow
from app.config import get_settings
from app.db import SessionLocal, init_db
from app.main import app
from app.models import CV, Company, Contact
from app.web.deps import get_llm
from app.web.i18n import TRANSLATIONS
from tests.fakes import FakeLLMProvider

client = TestClient(app)

LINKEDIN_URL = "https://www.linkedin.com/company/acme-linkedin"

FETCHED = {
    "slug": "acme-linkedin",
    "source_url": LINKEDIN_URL,
    "sections": {"about": "Acme. 11-50 employees.", "employees": "Ada Lovelace - CTO"},
    "people": [{"name": "Ada Lovelace", "profile_url": "https://www.linkedin.com/in/ada"}],
    "tool_errors": {},
}

LLM_ANSWER = json.dumps(
    {
        "employees_listed": "273 çalışan",
        "industry": "Software Development",
        "headquarters": "Ankara",
        "founded": "2018",
        "positions_summary": [{"group": "Engineering", "count": 9, "source": "linkedin_breakdown"}],
        "activity": {"last_post_date": "1w", "is_active": True, "assessment": "Posts weekly."},
        "jobs": [{"title": "Backend Engineer", "location": "Ankara"}],
        "key_people": [{"name": "Ada Lovelace", "role": "CTO"}],
    }
)


def setup_module():
    init_db()
    db = SessionLocal()
    try:
        if db.query(CV).first() is None:  # onboarding gate redirects everywhere without one
            db.add(CV(name="Test LinkedIn CV", file_path="data/cv_variants/test_linkedin.pdf"))
            db.commit()
    finally:
        db.close()


def teardown_module():
    app.dependency_overrides.pop(get_llm, None)


@pytest.fixture
def linkedin_on():
    settings = get_settings()
    previous = settings.linkedin_research_enabled
    settings.linkedin_research_enabled = True
    yield
    settings.linkedin_research_enabled = previous


@pytest.fixture
def fake_linkedin(monkeypatch):
    monkeypatch.setattr(
        linkedin_workflow,
        "fetch_linkedin_company",
        lambda url, **kwargs: FETCHED,
    )
    app.dependency_overrides[get_llm] = lambda: FakeLLMProvider(LLM_ANSWER)


def _company(name: str, **kwargs) -> int:
    db = SessionLocal()
    try:
        company = Company(name=name, **kwargs)
        db.add(company)
        db.commit()
        return company.id
    finally:
        db.close()


def _reload(company_id: int) -> Company:
    db = SessionLocal()
    try:
        return db.get(Company, company_id)
    finally:
        db.close()


def test_feature_is_invisible_and_inert_while_the_flag_is_off(fake_linkedin):
    company_id = _company("Flag Off Co", linkedin_url=LINKEDIN_URL)

    page = client.get(f"/companies/{company_id}")
    assert "linkedin-research" not in page.text
    assert 'name="linkedin_url"' not in page.text

    client.post(f"/companies/{company_id}/linkedin-research", follow_redirects=True)
    assert _reload(company_id).linkedin_research == {}


def test_research_renders_headcount_positions_activity_and_jobs(linkedin_on, fake_linkedin):
    company_id = _company("Acme LinkedIn Co", linkedin_url=LINKEDIN_URL)

    page = client.post(f"/companies/{company_id}/linkedin-research", follow_redirects=True)
    assert page.status_code == 200
    assert "273 çalışan" in page.text
    assert "Engineering" in page.text
    assert "Posts weekly." in page.text
    assert "Backend Engineer" in page.text
    assert "Ada Lovelace" in page.text


def test_run_button_hidden_until_a_linkedin_url_is_known(linkedin_on, fake_linkedin):
    company_id = _company("No LinkedIn Co")

    page = client.get(f"/companies/{company_id}")
    assert "linkedin-research" not in page.text

    client.post(
        f"/companies/{company_id}/details",
        data={"contact_email": "", "linkedin_url": LINKEDIN_URL, "notes": ""},
        follow_redirects=True,
    )
    assert _reload(company_id).linkedin_url == LINKEDIN_URL
    assert "linkedin-research" in client.get(f"/companies/{company_id}").text


def test_details_save_without_the_field_keeps_the_stored_url():
    """With the flag off the details form has no LinkedIn input at all - saving
    it must not wipe a URL that research already found."""
    company_id = _company("Keep URL Co", linkedin_url=LINKEDIN_URL)

    client.post(
        f"/companies/{company_id}/details",
        data={"contact_email": "hr@keepurl.example", "notes": "still here"},
        follow_redirects=True,
    )
    assert _reload(company_id).linkedin_url == LINKEDIN_URL


def test_linkedin_person_becomes_a_contact_that_needs_an_email_first(linkedin_on, fake_linkedin):
    company_id = _company("Escalation Co", linkedin_url=LINKEDIN_URL)
    client.post(f"/companies/{company_id}/linkedin-research", follow_redirects=True)

    page = client.post(
        f"/companies/{company_id}/contacts/suggested",
        data={
            "name": "Ada Lovelace",
            "role": "CTO",
            "email": "",
            "source_url": "https://www.linkedin.com/in/ada",
        },
        follow_redirects=True,
    )
    db = SessionLocal()
    try:
        contact = db.query(Contact).filter(Contact.company_id == company_id).one()
        contact_id = contact.id
        assert contact.email == ""
        assert contact.added_manually is False
        assert contact.source_url == "https://www.linkedin.com/in/ada"
    finally:
        db.close()

    # no address yet -> no escalation offered, an email form instead
    assert f"/contacts/{contact_id}/escalate" not in page.text
    assert f"/contacts/{contact_id}/email" in page.text

    page = client.post(
        f"/companies/{company_id}/contacts/{contact_id}/email",
        data={"email": "ada@escalation.example"},
        follow_redirects=True,
    )
    assert f"/contacts/{contact_id}/escalate" in page.text


def test_invalid_contact_email_is_not_saved(linkedin_on, fake_linkedin):
    company_id = _company("Bad Email Co", linkedin_url=LINKEDIN_URL)
    db = SessionLocal()
    try:
        contact = Contact(company_id=company_id, name="Grace Hopper", role="VP", email="")
        db.add(contact)
        db.commit()
        contact_id = contact.id
    finally:
        db.close()

    client.post(
        f"/companies/{company_id}/contacts/{contact_id}/email",
        data={"email": "not-an-email"},
        follow_redirects=True,
    )
    db = SessionLocal()
    try:
        assert db.get(Contact, contact_id).email == ""
    finally:
        db.close()


def test_failure_is_shown_on_the_page_instead_of_crashing(linkedin_on, monkeypatch):
    def boom(url, **kwargs):
        raise RuntimeError("session expired - run uvx mcp-server-linkedin@latest --login")

    monkeypatch.setattr(linkedin_workflow, "fetch_linkedin_company", boom)
    app.dependency_overrides[get_llm] = lambda: FakeLLMProvider("{}")
    company_id = _company("Broken Session Co", linkedin_url=LINKEDIN_URL)

    page = client.post(f"/companies/{company_id}/linkedin-research", follow_redirects=True)
    assert page.status_code == 200
    assert "session expired" in page.text


def test_site_and_linkedin_people_share_one_table(linkedin_on, fake_linkedin):
    """Two sources, one confirm step - the owner does the same thing with both."""
    company_id = _company("Merged People Co", linkedin_url=LINKEDIN_URL)
    db = SessionLocal()
    try:
        company = db.get(Company, company_id)
        company.research = {
            "pages": [{"url": "https://merged.example", "text": "hi"}],
            "suggested_contacts": [
                {"name": "Site Person", "role": "HR", "email": "hr@merged.example", "source_url": "https://merged.example"}
            ],
        }
        db.commit()
    finally:
        db.close()

    page = client.post(f"/companies/{company_id}/linkedin-research", follow_redirects=True)
    assert "Site Person" in page.text
    assert "Ada Lovelace" in page.text
    # One table, not two - the merged heading is rendered exactly once and
    # carries the combined count.
    assert page.text.count(TRANSLATIONS["en"]["company_detail.suggested_contacts_title"]) == 1
    assert "People worth contacting (2)" in page.text
    # each row says where it came from; the LinkedIn one links the profile
    assert "https://www.linkedin.com/in/ada" in page.text
    assert TRANSLATIONS["en"]["company_detail.source_site"] in page.text


def test_linkedin_url_can_be_pasted_into_the_card(linkedin_on, fake_linkedin):
    company_id = _company("Paste URL Co")

    page = client.get(f"/companies/{company_id}")
    assert f"/companies/{company_id}/linkedin-url" in page.text

    client.post(
        f"/companies/{company_id}/linkedin-url",
        data={"linkedin_url": LINKEDIN_URL},
        follow_redirects=True,
    )
    assert _reload(company_id).linkedin_url == LINKEDIN_URL


def test_find_on_site_button_fills_the_url_or_says_it_could_not(linkedin_on, fake_linkedin, monkeypatch):
    import app.web.routes.company_detail as company_detail

    company_id = _company("Lookup Co", website_url="https://lookup.example")
    monkeypatch.setattr(company_detail, "find_linkedin_url", lambda url: None)
    page = client.post(f"/companies/{company_id}/linkedin-url/find", follow_redirects=True)
    assert _reload(company_id).linkedin_url is None
    assert TRANSLATIONS["en"]["company_detail.linkedin.url_not_found"] in page.text

    monkeypatch.setattr(company_detail, "find_linkedin_url", lambda url: LINKEDIN_URL)
    client.post(f"/companies/{company_id}/linkedin-url/find", follow_redirects=True)
    assert _reload(company_id).linkedin_url == LINKEDIN_URL
