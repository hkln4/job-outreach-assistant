# Application Outreach Assistant

Personal tool that automates and standardizes job-application outreach: turns a
curated list of target companies into tailored, human-approved application
emails, tracks every application, and escalates to individual employees when a
company doesn't respond. Full spec in [CLAUDE.md](CLAUDE.md).

## Demo

![End-to-end demo: dashboard, profile, preferences, adding a company (auto research + preference check), drafting, reject-with-feedback, approve-and-send, blocked hard exclusion, and the follow-up queue](docs/demo.gif)

The walkthrough above runs the real UI end-to-end: dashboard action queue ->
profile & preferences -> adding a company (research + preference check run
automatically) -> AI draft with sources -> reject-with-feedback regeneration ->
approve & send -> hard-exclusion block -> lifecycle tracking and the follow-up
queue. All companies, names and emails in it are fictional demo data recorded
against a throwaway database with the LLM and Gmail stubbed out - nothing was
actually sent. (Recorded before the company-page layout was reworked, so that
page looks different now.)

## Status

The full v1 workflow (CLAUDE.md section 10) is implemented end-to-end, plus the
LinkedIn company research that section 11 had deferred:

- **Profile**: CV/LinkedIn-export/GitHub/site ingestion, coverage ledger,
  two-stage synthesis, and an optional owner-written cover letter template that
  drafts follow the structure/tone of. Once synthesized, the page shows a clean
  summary/skills/roles view; ingestion tooling collapses into "Manage sources."
- **Preferences**: hard/soft constraints, evidence-first checker. A hard match
  blocks drafting until you confirm it.
- **Companies**: adding one with a site auto-runs research - fetches home +
  careers/about/contact pages (plus a few individual job postings), writes a
  short AI summary, runs the preference check, picks up the company's LinkedIn
  page from their own social links, and auto-fills the contact email HR-first if
  it's on the site (never overwriting one you entered). There's also a **bulk
  add**: paste a whole list, one company per line in whatever loose format it's
  already in (`=`, `|`, `;`, comma or tab), preview what was parsed, then add
  them in one go with per-row status.
- **Dashboard**: the single place applications get started and reviewed - six
  action queues (needs your review / ready to draft / needs research / blocked
  by a preference match / awaiting reply / no reply yet), each with its action
  inline.
- **Company page**: applications first (with a one-line "you've contacted them
  before" note and a small expander to start another draft), then website
  research, then the LinkedIn card, then one table of people worth contacting
  that merges what the site and LinkedIn turned up - nothing becomes a `Contact`
  until you add it.
- **Drafting**: Evidence Rule (every company claim sourced), an "AI notes" panel
  explaining the company fit, which of your projects/skills matched, and the
  position+CV recommendation. Draft renders as an email (To/Subject/Body),
  editable in place. CV attachment is resolved case/whitespace-insensitively
  against your CV variants, with a visible warning + manual override if nothing
  matched (a CV must always be attached - see CLAUDE.md section 9). Drafting is
  refused outright when research came back empty, so the model is never asked to
  write about a blank page.
- **Review loop**: green Approve / red Reject side by side; Reject opens a
  compact chat-style feedback box that regenerates the draft via AI. Approve is
  disabled until a recipient email and a CV are both resolved, and editing the
  draft without saving raises an "unsaved changes" badge that blocks the send
  rather than quietly mailing the older text. A draft you started by mistake can
  be discarded (before sending only - sent applications are kept as history).
- **LinkedIn research** (optional, off by default): per company, how many people
  LinkedIn lists, what they do, whether the company still posts, what it lists
  as open, and who is worth contacting. See
  [LinkedIn research](#linkedin-research-optional).
- Gmail send with address validation, dedup and a send delay; reply checking
  (manual, bulk, and a quiet auto-check on page load); same-thread follow-ups;
  employee-contact escalation.
- EN/TR interface (cookie-based toggle), loading spinners on every
  LLM-triggering action so nothing looks frozen mid-request.
- 122 passing tests (`tests/`), including route-level end-to-end flow tests.

**To actually use it**, pick a provider in `.env` (`LLM_PROVIDER=gemini` or
`claude`) and set that provider's API key - everything that calls the LLM
(ingestion extraction, preference checks, research, drafting) needs it. Without
a key those routes fail on a friendly error page rather than doing anything
silently wrong. Gmail send/reply-check additionally need `credentials.json`
(OAuth client, see CLAUDE.md section 5); `token.json` is created on first send.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# set LLM_PROVIDER and the matching key:
#   gemini -> GEMINI_API_KEY (Google AI Studio)
#   claude -> ANTHROPIC_API_KEY
```

## Run

```bash
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000
```

Open http://127.0.0.1:8000 - localhost only, no auth (single-user tool). Use the
venv's interpreter: dependencies (including the MCP client the LinkedIn research
uses) are installed there, not in the system Python.

Tables are created automatically on first startup (`data/app.db`); schema changes
to existing tables are applied as safe, additive `ALTER TABLE` migrations in
`app/db.py` - existing data is never touched or rebuilt.

Suggested first run: **CVs** (add at least one variant) -> **Profile** (upload a
CV/LinkedIn export, ingest GitHub repos, synthesize, review/edit, optionally
paste a cover letter template) -> **Preferences** (add constraints) ->
**Companies** (add one with its website - research runs automatically) ->
**Dashboard** (start the application, review the draft, approve & send).

## LinkedIn research (optional)

Off by default. When enabled, each company page gets a LinkedIn card: how many
people LinkedIn actually lists (not the company's own size bracket, which is a
much larger number on most company pages), a breakdown of what they do, whether
the company has posted recently, the openings LinkedIn lists next to what their
own careers page said, and the people worth contacting. Those people flow into
the same confirm-before-it-counts table as site-found contacts; LinkedIn never
exposes email addresses, so they stay un-escalatable until you fill one in.

Decision-makers don't appear on the unfiltered employees page, so research also
searches that page for each term in `LINKEDIN_PEOPLE_KEYWORDS` (`CEO,CTO,müdür,
insan kaynakları,HR,founder` by default) - the same thing as typing them into
the people tab's search box. People whose title names a different employer are
dropped. Each term costs one more page load: shorten the list to go faster, or
empty it to skip the targeted search.

The company's LinkedIn URL is picked up automatically from the LinkedIn icon on
their own website during normal research. For companies added before that
existed, the card has a paste box and a "look for it on their website" button
that re-reads just their social links - no LLM, no LinkedIn traffic.

**Use a throwaway LinkedIn account.** This drives a real browser session, which
LinkedIn's terms forbid and which can get an account restricted. That is why the
feature is opt-in, runs one company per click (never on add, never in bulk), and
only ever reads company pages - it does not message anyone or send connection
requests.

```bash
# 1. install uv (provides uvx): https://docs.astral.sh/uv/getting-started/installation/
# 2. log the throwaway account in once - the session persists under ~/.linkedin-mcp/
uvx mcp-server-linkedin@latest --login
# 3. in .env:
#    LINKEDIN_RESEARCH_ENABLED=true
```

The server is [stickerdaniel/linkedin-mcp-server](https://github.com/stickerdaniel/linkedin-mcp-server),
published as `mcp-server-linkedin` (`linkedin-mcp-server` on PyPI is an
unrelated project). Keep the `@latest` tag - LinkedIn changes its page structure
often, and a pinned version quietly starts returning empty data.

A run takes a few minutes (a real browser loading eight LinkedIn pages) and the
request blocks for that long on purpose - it is one owner-triggered action, not
a background job. If the session expires, the company page shows the error and
re-running `--login` fixes it.

## Test

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

LLM-dependent logic is tested against `tests/fakes.py::FakeLLMProvider` /
`SequencedLLMProvider` (canned JSON responses) - no real API key needed to run
the suite. Gmail send is monkeypatched; it is never called for real during
`pytest`. The LinkedIn MCP subprocess is never spawned - `linkedin_fetch` is
faked at the same seam as the website fetch. `tests/conftest.py` points the app
at a throwaway SQLite file before any app module is imported and forces the
LinkedIn flag off, so the suite never touches the real `data/app.db` and never
depends on your own `.env`.

## Layout

```
app/
  config.py            settings from .env (pydantic-settings)
  db.py                 SQLAlchemy engine/session, init_db(), column migrations
  models.py              entities + state machines (CLAUDE.md sections 6-7)
  main.py                 FastAPI app entrypoint, onboarding gate, error handlers
  llm/                    provider-agnostic LLM interface, Gemini + Claude (section 8)
  profile_builder/        CV/LinkedIn/GitHub/site ingestion, coverage ledger, synthesis
  preferences/             hard/soft constraint checker (section 4)
  companies/                site fetch, research workflow, bulk-list parsing,
                             LinkedIn fetch + research (sections 5, 9)
  applications/               drafting, CV resolution, review/approve/reject,
                               follow-ups, reply check, escalation (section 5)
  email_send/                  Gmail OAuth2 send + reply check (sections 5, 9)
  web/                           FastAPI routes + Jinja2 templates + i18n + a
                                  little JS (spinners, bulk add, unsaved-draft guard)
scripts/
  init_db.py            manual DB init
  gmail_smoke_test.py    manual OAuth + send verification
  send_test_email.py      send a real test mail (for the reply-check flow)
  demo/                    throwaway DB + stub server used to record the demo GIF
tests/                 pytest suite (122 tests)
```
