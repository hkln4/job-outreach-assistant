from fastapi.testclient import TestClient

from app.db import SessionLocal, init_db
from app.main import app
from app.models import CV
from app.web.errors import classify_llm_error


class _StatusError(Exception):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


def test_classifies_rate_limit_by_status_code():
    assert classify_llm_error(_StatusError("too many requests", 429)) == "rate_limit"


def test_classifies_billing_by_message_even_without_status_match():
    # the real anthropic.BadRequestError this was built for: status 400, but
    # the message is what actually says "no credit"
    exc = _StatusError(
        "Your credit balance is too low to access the Anthropic API.", 400
    )
    assert classify_llm_error(exc) == "billing"


def test_classifies_auth_by_status_code():
    assert classify_llm_error(_StatusError("invalid x-api-key", 401)) == "auth"


def test_unknown_error_falls_back():
    assert classify_llm_error(ValueError("No analyzed ProfileItems yet")) == "unknown"


def test_unhandled_exception_renders_friendly_page_not_a_crash():
    """Regression: previously an unhandled exception (e.g. no analyzed items,
    or any LLM error) propagated as a raw traceback through the custom
    onboarding middleware instead of a rendered page."""
    init_db()
    db = SessionLocal()
    try:
        if db.query(CV).first() is None:
            db.add(CV(name="Test CV", file_path="data/cv_variants/test.pdf"))
            db.commit()
    finally:
        db.close()

    # raise_server_exceptions=False: TestClient's default re-raises unhandled
    # exceptions into the test process for debugging visibility, which would
    # defeat the point of this test (it should see the same rendered response
    # a real browser gets - already confirmed by hand against a live server).
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/profile/synthesize")
    assert r.status_code == 500
    assert "No analyzed ProfileItems yet" in r.text
    assert "Traceback" not in r.text
