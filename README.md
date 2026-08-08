# Application Outreach Assistant

Personal tool that automates and standardizes job-application outreach: turns a
curated list of target companies into tailored, human-approved application
emails, tracks every application, and escalates to individual employees when a
company doesn't respond. Full spec in [CLAUDE.md](CLAUDE.md).

## Status

Full v1 workflow (CLAUDE.md section 10) is implemented end-to-end, plus a
dashboard-centric UI redesign on top of it:

- **Profile**: CV/LinkedIn/GitHub/site ingestion, coverage ledger, two-stage
  synthesis, and an optional owner-written cover letter template that drafts
  follow the structure/tone of. Once synthesized, the page shows a clean
  summary/skills/roles view; ingestion tooling collapses into "Manage sources."
- **Preferences**: hard/soft constraints, evidence-first checker.
- **Companies**: adding a company with a site auto-runs research immediately -
  fetches home + careers/about/contact pages, writes a short AI summary of the
  company, runs the preference check, and tries to auto-fill the contact email
  from the page text (never overwrites one you entered yourself). No manual
  "run research" step needed, though a "Re-run research" button exists.
- **Dashboard**: the single place applications get started and reviewed - four
  action-queue sections (needs your review / ready to draft / needs research /
  blocked by a preference match), each with the action inline.
- **Drafting**: Evidence Rule (every company claim sourced), an "AI notes"
  panel explaining the company fit, which of your projects/skills matched, and
  the position+CV recommendation. Draft renders as an email (To/Subject/Body),
  editable in place. CV attachment is resolved case/whitespace-insensitively
  against your CV variants, with a visible warning + manual override if nothing
  matched (a CV must always be attached - see CLAUDE.md section 9).
- **Review loop**: green Approve / red Reject side by side; Reject opens a
  compact chat-style feedback box (send button, not a wall of text) that
  regenerates the draft via AI. Approve is disabled until a recipient email and
  a CV are both resolved.
- Gmail send with dedup, reply checking, employee-contact escalation.
- EN/TR interface (cookie-based toggle), loading spinners on every
  LLM-triggering action so nothing looks frozen mid-request.
- 40 passing tests (`tests/`), including route-level end-to-end flow tests.

**To actually use it**, set `GEMINI_API_KEY` in `.env` - everything that calls
the LLM (ingestion extraction, preference checks, drafting) needs it; without
it those routes fail cleanly with a friendly error page rather than doing
anything silently wrong. Gmail send/reply-check need `credentials.json` (OAuth
client, see CLAUDE.md section 5) and a completed OAuth consent flow
(`token.json` gets created on first send).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# fill in GEMINI_API_KEY at minimum (Google AI Studio)
```

## Run

```bash
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000
```

Open http://127.0.0.1:8000 - localhost only, no auth (single-user tool).
Tables are created automatically on first startup (`data/app.db`); schema
changes to existing tables are applied as safe, additive `ALTER TABLE`
migrations in `app/db.py` - existing data is never touched or rebuilt.

Suggested first run: **CVs** (add at least one variant) -> **Profile** (upload
a CV/LinkedIn export, ingest GitHub repos, synthesize, review/edit, optionally
paste a cover letter template) -> **Preferences** (add constraints) ->
**Companies** (add one with its website - research runs automatically) ->
**Dashboard** (start the application, review the draft, approve & send).

## Test

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

LLM-dependent logic is tested against `tests/fakes.py::FakeLLMProvider` /
`SequencedLLMProvider` (canned JSON responses) - no real API key needed to run
the suite. Gmail send is monkeypatched in tests; it is never called for real
during `pytest`. `tests/conftest.py` points the app at a throwaway SQLite file
before any app module is imported, so the test suite never touches the real
`data/app.db`.

## Layout

```
app/
  config.py            settings from .env (pydantic-settings)
  db.py                 SQLAlchemy engine/session, init_db(), column migrations
  models.py              entities + state machines (CLAUDE.md sections 6-7)
  main.py                 FastAPI app entrypoint, onboarding gate, error handlers
  llm/                    provider-agnostic LLM interface (section 8)
  profile_builder/        CV/LinkedIn/GitHub/site ingestion, coverage ledger, synthesis
  preferences/             hard/soft constraint checker (section 4)
  companies/                site+careers/about/contact fetch, research workflow (section 5)
  applications/               drafting, CV resolution, review/approve/reject, escalation (section 5)
  email_send/                  Gmail OAuth2 send + reply check (section 5, 9)
  web/                           FastAPI routes + Jinja2 templates + i18n (review UI)
scripts/
  init_db.py            manual DB init
  gmail_smoke_test.py    manual OAuth + send verification
tests/                 pytest suite (40 tests)
```
