"""
Per-company research (CLAUDE.md section 5, steps 1-2). CRUD for Company (state:
APPLICABLE/ON_HOLD/REJECTED) is wired in app/web/routes/companies.py.

Planned: fetch.py - given Company.website_url, fetch the site + careers page,
extract main text (not raw HTML, readability-lxml), trim it, cache into
Company.research. Retrieval stays in the app - never the model's own web-search
tool. Then runs app.preferences.checker against the extracted text.
"""
