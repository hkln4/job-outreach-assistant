"""Cross-site POST / DNS-rebinding guard (app/web/security.py). Uses starlette's
TestClient directly: conftest.py swaps fastapi's for one that marks every
request same-origin, and these tests need to control that header themselves."""
import pytest
from starlette.testclient import TestClient

import app.applications.review as review
from app.db import SessionLocal, init_db
from app.main import app
from app.models import CV, Application, ApplicationState, Company

client = TestClient(app)
ORIGINAL_BODY = "Dear CSRF Co, ..."


def setup_module():
    init_db()
    db = SessionLocal()
    try:
        if db.query(CV).first() is None:  # onboarding gate redirects everywhere without one
            db.add(CV(name="CSRF CV", file_path="data/cv_variants/csrf.pdf"))
            db.commit()
    finally:
        db.close()


@pytest.fixture
def application_id():
    db = SessionLocal()
    try:
        company = Company(name="CSRF Co", website_url="https://csrf.example", contact_email="jobs@csrf.example")
        db.add(company)
        db.commit()
        application = Application(
            company_id=company.id,
            subject="Application",
            cover_letter_body=ORIGINAL_BODY,
            state=ApplicationState.PENDING_APPROVAL,
        )
        db.add(application)
        db.commit()
        return application.id
    finally:
        db.close()


@pytest.fixture
def sends(monkeypatch):
    calls = []

    def fake_send(**kwargs):
        calls.append(kwargs)
        return {"id": f"msg-{len(calls)}", "threadId": f"thread-{len(calls)}"}

    monkeypatch.setattr(review, "send_email", fake_send)
    return calls


def _body(application_id: int) -> str:
    db = SessionLocal()
    try:
        return db.get(Application, application_id).cover_letter_body
    finally:
        db.close()


def _edit(application_id: int, headers: dict):
    return client.post(
        f"/applications/{application_id}/edit",
        data={"subject": "Attacker subject", "cover_letter_body": "Attacker body"},
        headers=headers,
        follow_redirects=False,
    )


def test_cross_site_approve_does_not_send(application_id, sends):
    r = client.post(
        f"/applications/{application_id}/approve",
        data={"force": "1"},
        headers={"sec-fetch-site": "cross-site"},
        follow_redirects=False,
    )

    assert r.status_code == 403
    assert "Request blocked" in r.text
    assert sends == []


def test_same_site_is_not_same_origin(application_id, sends):
    """Another port on localhost is same-site but a different origin."""
    r = client.post(
        f"/applications/{application_id}/approve", headers={"sec-fetch-site": "same-site"}, follow_redirects=False
    )
    assert r.status_code == 403
    assert sends == []


def test_cross_site_edit_does_not_change_the_draft(application_id):
    r = _edit(application_id, {"sec-fetch-site": "cross-site"})

    assert r.status_code == 403
    assert _body(application_id) == ORIGINAL_BODY


def test_same_origin_edit_works(application_id):
    r = _edit(application_id, {"sec-fetch-site": "same-origin"})

    assert r.status_code == 303
    assert _body(application_id) == "Attacker body"


def test_same_origin_approve_sends(application_id, sends):
    r = client.post(
        f"/applications/{application_id}/approve", headers={"sec-fetch-site": "same-origin"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert len(sends) == 1


def test_matching_origin_is_allowed_without_sec_fetch_site(application_id):
    r = _edit(application_id, {"origin": "http://testserver"})

    assert r.status_code == 303
    assert _body(application_id) == "Attacker body"


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": "https://evil.example"},
        {"origin": "http://testserver.evil.example"},
        {"origin": "https://testserver"},  # same host, other scheme
        {"origin": "null"},
        {"referer": "https://evil.example/page"},
        {},  # neither Origin nor Referer
    ],
)
def test_foreign_or_missing_origin_is_refused(application_id, headers):
    r = _edit(application_id, headers)

    assert r.status_code == 403
    assert _body(application_id) == ORIGINAL_BODY


def test_matching_referer_is_allowed_without_origin(application_id):
    r = _edit(application_id, {"referer": f"http://testserver/applications/{application_id}"})

    assert r.status_code == 303
    assert _body(application_id) == "Attacker body"


def test_get_is_never_blocked_by_the_origin_check(application_id):
    r = client.get(f"/applications/{application_id}", headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 200


@pytest.mark.parametrize("method", ["get", "post"])
def test_foreign_host_is_refused(method):
    """DNS rebinding: the attacker's page reaches 127.0.0.1 under its own hostname."""
    r = client.request(
        method, "/", headers={"host": "rebind.evil.example", "sec-fetch-site": "same-origin"}, follow_redirects=False
    )
    assert r.status_code == 403


def test_localhost_with_port_is_an_allowed_host(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "allowed_hosts", "127.0.0.1,localhost")
    for host in ("127.0.0.1:8000", "localhost:8000"):
        r = client.get("/", headers={"host": host}, follow_redirects=False)
        assert r.status_code != 403, host


@pytest.mark.parametrize(
    "referer, expected",
    [
        ("http://testserver/companies?state=APPLICABLE", "/companies?state=APPLICABLE"),
        ("https://evil.example/phish", "/"),
        ("//evil.example/phish", "/"),
        (None, "/"),
    ],
)
def test_language_switch_only_redirects_within_the_app(referer, expected):
    headers = {"referer": referer} if referer else {}
    r = client.get("/set-lang/en", headers=headers, follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == expected
