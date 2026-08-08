# Application Outreach Assistant

Personal tool that automates and standardizes job-application outreach: turns a
curated list of target companies into tailored, human-approved application
emails, tracks every application, and escalates to individual employees when a
company doesn't respond. Full spec in [CLAUDE.md](CLAUDE.md).

## Status

Scaffolding stage. Working today: company list CRUD (add a company, set its
state) backed by a real SQLite DB, and the provider-agnostic LLM layer
(Gemini active, Claude wired but inactive). Everything else in the v1 workflow
- Profile Builder, preference checks, drafting, Gmail send - is stubbed out
with docstrings pointing at the relevant CLAUDE.md section; see `app/*/__init__.py`.

Build order follows CLAUDE.md section 10.

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
Tables are created automatically on first startup (`data/app.db`).

## Test

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

## Layout

```
app/
  config.py            settings from .env (pydantic-settings)
  db.py                 SQLAlchemy engine/session, init_db()
  models.py              entities + state machines (CLAUDE.md sections 6-7)
  main.py                 FastAPI app entrypoint
  llm/                    provider-agnostic LLM interface (section 8)
  profile_builder/        TODO - one-time onboarding (section 3)
  preferences/             TODO - hard/soft constraint checker (section 4)
  companies/                TODO - site fetch + research caching (section 5)
  applications/               TODO - recommend/draft/review (section 5)
  email_send/                  TODO - Gmail send (section 5, 9)
  web/                           FastAPI routes + Jinja2 templates (review UI)
scripts/init_db.py    manual DB init
tests/                 pytest suite
```
