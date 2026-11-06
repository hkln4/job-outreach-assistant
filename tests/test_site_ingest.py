from app.db import SessionLocal, init_db
from app.models import ProfileItemStatus
from app.profile_builder.site_ingest import ingest_site
from tests.fakes import FakeLLMProvider


def setup_module():
    init_db()


def test_ingest_site_survives_unreachable_url_instead_of_crashing():
    """Regression: a bad URL (DNS failure, timeout, 404) must become a
    needs_review item, not an unhandled exception that 500s the request."""
    llm = FakeLLMProvider("should never be called")
    db = SessionLocal()
    try:
        item = ingest_site(db, llm, "https://this-domain-does-not-exist-xyz123.invalid")
        assert item.status == ProfileItemStatus.NEEDS_REVIEW
        assert "parse_error" in item.extracted
        assert len(llm.calls) == 0
    finally:
        db.close()


def test_portfolio_project_subpages_become_their_own_items(monkeypatch):
    import json

    import httpx

    import app.profile_builder.site_ingest as site_ingest
    from app.models import ProfileItem

    body = "<p>" + "A detailed write-up about this project and what it does. " * 10 + "</p>"
    pages = {
        "/": f'<html><body>{body}<a href="/projects/lesion-seg">LesionSeg</a><a href="/projects/etl">ETL</a>'
        '<a href="https://github.com/owner">GitHub</a><a href="/contact">Contact</a></body></html>',
        "/projects/lesion-seg": f"<html><body><h1>LesionSeg</h1>{body}</body></html>",
        "/projects/etl": f"<html><body><h1>ETL</h1>{body}</body></html>",
    }

    def fake_get(url, **kwargs):
        path = httpx.URL(url).path
        return httpx.Response(200, text=pages[path], request=httpx.Request("GET", url))

    monkeypatch.setattr(site_ingest.httpx, "get", fake_get)
    llm = FakeLLMProvider(json.dumps({"purpose": "Project page", "stack": [], "role": "author", "highlights": []}))
    db = SessionLocal()
    try:
        site_ingest.ingest_site(db, llm, "https://owner.example/", follow_subpages=True)
        children = db.query(ProfileItem).filter(ProfileItem.parent_ref == "https://owner.example/").all()
        assert sorted(c.source_ref for c in children) == ["https://owner.example/projects/etl", "https://owner.example/projects/lesion-seg"]
        assert len(llm.calls) == 3  # one per page, the GitHub/contact links not followed
    finally:
        db.close()
