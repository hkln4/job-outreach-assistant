"""
Point the app at a throwaway SQLite file before any app module is imported,
so tests never touch data/app.db. Must run before test modules import app.*.
"""
import os
import tempfile

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_db.name}"
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")
# Sent-CV snapshots and test CV files live in a temp dir, never under data/.
TEST_FILES_DIR = tempfile.mkdtemp(prefix="outreach-tests-")
os.environ["SENT_CVS_DIR"] = os.path.join(TEST_FILES_DIR, "sent_cvs")
os.environ["CV_VARIANTS_DIR"] = os.path.join(TEST_FILES_DIR, "cv_variants")
os.environ["GENERATED_CVS_DIR"] = os.path.join(TEST_FILES_DIR, "generated_cvs")
os.environ["DISCOVERY_UPLOADS_DIR"] = os.path.join(TEST_FILES_DIR, "discovery_uploads")
# Tests send back-to-back; the deliverability gap is exercised explicitly in
# test_outreach.py with a patched Settings instead.
os.environ.setdefault("SEND_DELAY_SECONDS", "0")
# Always start from the off state regardless of the owner's own .env (an env
# var outranks the .env file), so the "feature is invisible when disabled"
# tests test the code, not the local config. test_web_linkedin.py flips the
# Settings object per test to cover the on state.
os.environ["LINKEDIN_RESEARCH_ENABLED"] = "false"
# Same for the reviewer pass on drafts: off unless a test turns it on, so the
# drafting tests' canned LLM sequences aren't shifted by an extra call and no
# real provider is built for it. test_draft_review.py covers the on state.
os.environ["DRAFT_REVIEW_ENABLED"] = "false"
# The cross-site guard (app/web/security.py) only answers to allowed hosts;
# TestClient addresses the app as "testserver".
os.environ["ALLOWED_HOSTS"] = "127.0.0.1,localhost,testserver"

# Every route test acts like a click on the app's own page. A real browser
# marks those requests same-origin; TestClient sends no such header, so add it
# once here. test_csrf.py uses starlette's TestClient directly to test without it.
import fastapi.testclient  # noqa: E402
from starlette.testclient import TestClient as _StarletteTestClient  # noqa: E402


class _SameOriginTestClient(_StarletteTestClient):
    def __init__(self, *args, headers=None, **kwargs):
        super().__init__(*args, headers={"sec-fetch-site": "same-origin", **(headers or {})}, **kwargs)


fastapi.testclient.TestClient = _SameOriginTestClient


# No test may reach the real Gmail API: the OAuth flow would open a browser.
# Tests fake Gmail at the function they exercise (send_email, classify_thread,
# find_sent_message, ...); anything that slips through fails loudly here.
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_gmail(monkeypatch):
    import app.email_send.gmail_client as gmail_client

    def _refuse():
        raise RuntimeError("Gmail is not available in tests - fake the gmail_client function you need.")

    monkeypatch.setattr(gmail_client, "get_gmail_service", _refuse)


def make_cv_file(name: str = "cv.pdf", content: bytes = b"%PDF-1.4 test cv") -> str:
    """A real file for a test CV (sending copies it into cv_snapshot)."""
    path = os.path.join(TEST_FILES_DIR, name)
    with open(path, "wb") as f:
        f.write(content)
    return path


# No test may reach a real LLM either. Routes get a provider that refuses
# unless the test overrides get_llm / get_draft_llm itself (setdefault keeps
# a test's own override; tests that pop theirs get this one back next test).
@pytest.fixture(autouse=True)
def _no_real_llm():
    from app.llm.base import LLMProvider
    from app.main import app
    from app.web.deps import get_draft_llm, get_llm

    class _RefusingProvider(LLMProvider):
        def generate(self, prompt, *, system=None, temperature=0.2, max_output_tokens=2048, json_schema=None):
            raise RuntimeError("No real LLM in tests - override get_llm / get_draft_llm with a fake.")

    refusing = _RefusingProvider()
    added = []
    for dependency in (get_llm, get_draft_llm):
        if dependency not in app.dependency_overrides:
            app.dependency_overrides[dependency] = lambda: refusing
            added.append(dependency)
    yield
    for dependency in added:
        if app.dependency_overrides.get(dependency) is not None and dependency in added:
            app.dependency_overrides.pop(dependency, None)
