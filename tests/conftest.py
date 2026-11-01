"""
Point the app at a throwaway SQLite file before any app module is imported,
so tests never touch data/app.db. Must run before test modules import app.*.
"""
import os
import tempfile

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_db.name}"
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")
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
