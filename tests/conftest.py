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
