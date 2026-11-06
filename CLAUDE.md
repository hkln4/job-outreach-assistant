# CLAUDE.md

Project guide for the coding agent. Read this fully before working on the project.
Sections 1-9 describe the system and the rules that never change. A rule tagged
**(phase N)** belongs to a roadmap phase - check its status in `docs/ROADMAP.md` before
assuming the code already does it. Section 10 is what is already built. Section 11 is the
roadmap summary (full specs in `docs/ROADMAP.md`) - work on the earliest unfinished phase
unless the owner asks for something else. Section 12 is what must not be built without the
owner asking.

---

## 1. Project Overview

- **Purpose:** A personal tool that finds tech companies and job postings (primarily Ankara), turns them into tailored, human-approved applications, tracks every application, and escalates to individual employees when a company does not respond.
- **Primary goal:** actual personal use. Secondary: portfolio project. When trade-offs arise, favor a working, reliable tool over architectural showmanship - but keep it clean enough to show.
- **Single user** (the owner). No multi-tenant auth or account system.
- **Language:** code, identifiers, comments, prompts and stored records are in **English**. The UI is English with a Turkish toggle (labels only, `app/web/i18n.py` - every new key gets both languages). **Outgoing letters, form answers and generated CVs are the exception:** Turkish or English. The letter language comes from `OwnerSettings.email_language` (Turkish / English / no preference). With no preference: the language of the job posting if there is one, else the cover-letter template's language, else Turkish. Follow-ups reuse the language of the original email.
- Two entry points feed the same pipeline:
  - **Company-first:** a curated company list -> research -> open application.
  - **Posting-first (phase 3):** a specific job posting -> its company -> an application aimed at that posting.

---

## 2. Shape of the System

A normal application, not an agent-in-a-terminal:

- **Backend** owns all logic and state, makes the LLM calls, fetches web pages, renders CVs, and sends email.
- **Review UI** (FastAPI + Jinja2, localhost only) is where the owner reads drafts, approves/rejects, and manages companies, postings, profile, CVs and preferences.
- **LLM** is called through the backend via a **provider-agnostic interface** (section 8).

The owner drives it through the UI; the backend does the work. There is no autonomous background service - every action is owner-triggered, and every send or submission is gated by explicit approval.

---

## 3. Profile Builder (Onboarding)

Goal: one durable, detailed, reviewed **profile of the owner**, used as context in every letter, form answer and CV. Run once at setup; re-runnable whenever new work is added.

**Core principle: completeness is the app's job, not the model's.** The app enumerates discrete items from each source and processes them one at a time. The model never decides which items to look at.

### Sources & ingestion
- **Uploaded CVs** (PDF/docx): each file is one source; phase 1 splits it into one record per role / project / education entry.
- **GitHub** (official API): list ALL repos deterministically, pre-filter forks/empty repos, owner confirms include/exclude. Per repo: README, description, topics, languages, file tree + sampled key source files, stars, recent commits.
- **LinkedIn**: the owner uploads a profile PDF export or data archive. The owner's own LinkedIn data is never scraped.
- **Extra sites** (portfolio, blog): fetch, extract main text, optionally follow project sub-pages with a depth cap.

### Per-item deep pass
- One item per LLM call, structured output (`json_schema`). Unknowns are explicit ("unknown"), never invented. Uncertain items are `needs_review`.

### Coverage ledger
- Manifest of every item found and its status (`analyzed` / `skipped` / `needs_review`), e.g. "42 repos found, 42 analyzed". Nothing is silently dropped.

### Two-stage synthesis
- Stage 1: per-item extraction. Stage 2: synthesize summary, skills-with-evidence, projects, experience, education, target roles **from the item records**, each claim tagged with its source. When the records exceed one prompt's budget, synthesis is hierarchical (groups, then merge) - every analyzed record reaches some synthesis call.

### Review
- The owner reviews/edits the profile and item records before they are saved. Owner edits to a record are marked and survive re-analysis. The owner is the final authority on facts about himself.

### Depth and freshness (phase 1)
- The profile is the ceiling for every letter, form answer and CV: a fact the profile doesn't hold cannot appear in any of them. Item records must therefore be **detailed and structured** - see `docs/ROADMAP.md` phase 1 for the record schema.
- Each repo record stores the version it was built from (`pushed_at`, HEAD SHA). A repo that changed since its analysis is shown as "changed - re-analyze"; re-analysis is owner-triggered, per repo or "all changed", and only changed repos cost a call.
- Repos rarely say what the owner most needs to claim (his role, team size, why it was built, the real result). The app asks him a few targeted questions per project and stores the answers as owner-stated facts.

### Fact capture (phase 4)
- Facts the owner states later (in reject feedback, form answers, CV edits) must reach the profile, or the grounding checks will treat them as invented next time. The app proposes them as `ProfileFact` records; nothing is added without the owner's approval.

---

## 4. Preferences & Constraints (owner)

Checked against every company (and every posting) **before** drafting.

- **Hard exclusions (dealbreakers)** - e.g. defense industry. A match **blocks drafting** with a prominent warning until the owner confirms (a knob can set it to warn-only).
- **Soft preferences** - e.g. prefer remote, avoid pure consultancy. Surfaced as a note, not a block.

The check passes extracted page text + preferences to the LLM. Output is **evidence-first**: each flag returns the snippet, **the URL of the page the snippet is actually on** (located by code; unverified ones are marked), severity and confidence. It is a flag for the owner's judgment, not a verdict.

Scope: preferences cover things to **avoid or require**. Positive role targeting lives in the profile's `target_roles`; do not duplicate it here.

---

## 5. End-to-End Workflow

### 5a. Company-first
1. **Company list.** Added by hand, by bulk paste, or approved from Discovery (5c). Each company has a `state`; the system only drafts for `APPLICABLE`. LinkedIn research (section 9) is an optional aid to vetting.
2. **Research.** The website URL is owner-provided or comes from a directory (never guessed). The backend fetches home + careers + about + contact (+ up to 3 job pages), sanitizes, extracts text, and the LLM summarizes. **The preference check runs here.** Emails and application channels (forms, ATS boards) are detected **deterministically from raw HTML**, not inferred from extracted text. Retrieval stays in the app - never the model's own web search.
3. **Recommendation.** Position + CV variant, with written reasoning.
4. **Draft.** Company-specific letter grounded in the profile. Every company claim has a source URL (Evidence Rule).
5. **Review.** Approve -> send with the correct CV. Reject + free-text feedback -> regenerate. A reviewer pass (`app/applications/draft_review.py`, `DRAFT_REVIEW_ENABLED`) critiques every draft but never blocks a send.
6. **Track.** Lifecycle states (section 7). Past applications are retained with their final state.
7. **Escalate.** If the generic address doesn't reply within the threshold, the owner can email specific employees. Contacts are entered by the owner or confirmed from people named on the company's own pages / LinkedIn research - never created automatically.

### 5b. Posting-first (phase 3)
1. A `JobPosting` enters by owner paste (URL or text), by a careers-page change, or from a public ATS board.
2. The posting is parsed into structured fields and matched to a Company (existing, or a proposed new one the owner approves).
3. Fit evaluation with gates (language, location, seniority) and per-dimension evidence.
4. Draft (letter or form answers) uses the **posting text as the primary company text**; a tailored CV may be assembled (phase 5).
5. Same review / send / track loop as 5a, linked by `Application.job_posting_id`.

### 5c. Discovery
Directory import -> **liveness check (no LLM, phase 2)** -> preference keyword pre-screen (phase 2) -> homepage screening (LLM) -> owner approves -> Company.

---

## 6. Entities / Data Model

Existing (see `app/models.py`): **Profile**, **ProfileItem**, **Preference**, **OwnerSettings**, **CV**, **Company**, **Application**, **FollowUp**, **Contact**, **DiscoverySource**, **CompanyCandidate**, **LLMCallLog**.

Added by the roadmap (fields are a guide, not a contract):

- **LLMCallLog** (phase 0) - `purpose`, `model`, `input_tokens`, `output_tokens`, `company_id` / `application_id` / `profile_item_id`, `created_at`.
- **Application** (phase 0) - `cv_snapshot` (`{path, sha256, name}` of the exact file attached), `rfc_message_id`, `notes[]` (auto-replies, possible replies).
- **ProfileItem** additions (phase 0/1) - `facts` (deterministic repo facts), `source_version` (`{pushed_at, head_sha, analyzed_at}`), `owner_facts[]` (answers to open questions, `source: "owner"`), `owner_edited_fields[]`, `parent_ref` (a CV/LinkedIn entry's source file), `pending_update` (re-analysis diff awaiting the owner).
- **JobPosting** (phase 3) - `id`, `company_id` (nullable until matched), `source` (`manual` | `careers_page` | `ats_api` | `linkedin`), `url`, `title`, `location`, `work_mode`, `language`, `description_text` (sanitized, full), `requirements[]`, `nice_to_have[]`, `deadline`, `content_hash`, `first_seen_at`, `last_seen_at`, `status`, `fit`, `legitimacy_flags[]`.
- **Application.job_posting_id** (phase 3), **Application.channel** (`email` | `site_form` | `ats_form` | `contact_form`, phase 6).
- **Detected channels** (phase 0, `research.channels_detected`) - `kind` (`site_form` | `ats_board` | `contact_form` | `portal`), `url`, `provider`, `token`, `form_schema`, `evidence_snippet`, `page_url`.
- **Liveness** JSON on `CompanyCandidate.liveness` and `Company.liveness` (phase 2) - `{status, signals[{name, value, evidence, url}], checked_at}`.
- **Bullet** (phase 5) - `profile_item_id`, `text`, `tags` (tech, domain, role_type), `source` (quote or owner fact), `status` (`draft` / `approved`), `locked`, `pending_text`.
- **TechSkill** (phase 5) - `name`, `group`, `evidence[]` (where it is used), `manual`.
- **GeneratedCV** (phase 5) - `application_id`, `selection`, `language`, `pdf_path`, `sha256`, `ats_report`, `approved_at`.
- **OwnerSettings** additions (phases 4-5) - name, title, phone, city, email, LinkedIn, GitHub, portfolio URL (signature and CV contact row).
- **StyleExample / StyleNote** (phase 4), **ProfileFact** (phase 4, `proposed` / `approved` / `rejected`).

---

## 7. State Machines

**Company.state**: `APPLICABLE` / `ON_HOLD` / `REJECTED` - drafting only for `APPLICABLE`.

**Application.state**: `DRAFT` -> `PENDING_APPROVAL` (<-> regenerate) -> `AWAITING_REPLY` -> one of { `REPLIED`, `INTERVIEW`, `REJECTED`, `NO_RESPONSE`, `BOUNCED` }. `BOUNCED` = delivery failed; the address is marked invalid and the owner is offered the next address. An auto-reply is **not** a reply: it is recorded as a note and the state stays `AWAITING_REPLY`. Form submissions (phase 6) enter `AWAITING_REPLY` only after the owner marks them submitted.

**JobPosting.status** (phase 3): `NEW` -> `SHORTLISTED` -> `APPLIED`; `EXPIRED` or `DISMISSED` from any open state.

**ProfileItem.status**: `analyzed` / `skipped` / `needs_review`.

**Employee escalation** reuses the application lifecycle, triggered from `NO_RESPONSE` past the threshold.

---

## 8. LLM Layer (provider-agnostic)

- All model calls go through **one internal interface** (`LLMProvider.generate(...)`). No provider SDK in business logic. `app/llm/factory.py` is the only place that branches on the provider name.
- **Providers:** `LLM_PROVIDER` = `gemini` | `claude`. Code default `gemini`; the owner runs `claude` via `.env`.
- **Two models per provider:** a cheap **reading model** (research, extraction, evidence selection, preference checks, triage, posting parsing) via `get_llm_provider`, and a stronger **draft model** via `get_draft_llm_provider` for anything a company will read (letters, follow-ups, escalations, form answers (phase 6), CV summaries (phase 5), angle selection and the reviewer pass (phase 4)) and for the one-time per-repo profile record (phase 1), since every later letter depends on it.
  - Claude: `ANTHROPIC_MODEL` (default `claude-haiku-4-5`), `ANTHROPIC_DRAFT_MODEL` (default `claude-sonnet-5-5`).
  - Gemini: `GEMINI_MODEL` (default `gemini-3.6-flash`), `GEMINI_DRAFT_MODEL` (empty = `GEMINI_MODEL`). The Interactions API takes no `temperature`; `temperature=0` calls get a fixed `seed`.
- **Structured output:** `generate(..., json_schema=Model.model_json_schema())` uses the provider's native mode; callers still parse with `parse_json_object` and validate with Pydantic. **Exception (phase 4):** prose the company reads (letter body) is generated as plain text, not inside JSON - metadata comes from a separate structured call.
- **Deterministic first - the LLM is the last, smallest step.** Every pipeline filters and narrows with plain code before any model call, and a model call happens only when what is left actually needs language understanding:
  - Discovery: dedup (`CompanyIndex`) -> rejected-before -> liveness (phase 2) -> preference **keyword pre-screen** on the homepage text (`Preference.signals`, phase 2) -> only then LLM screening. A keyword hit is shown as a flag; it never replaces the owner's decision.
  - Research: emails, channels, LinkedIn URL, careers link and job-page links are found by code; the LLM only summarizes and extracts positions. Skip re-research unless the owner asks or the content hash changed (phase 3 careers watch).
  - Postings (phase 3): dedup by URL and `content_hash`; location / language / seniority gates by regex where the text is explicit; expired postings dropped by HTTP status/patterns; only survivors get LLM fit evaluation.
  - Profile (phase 1): GitHub change detection by `pushed_at` + HEAD SHA - an unchanged repo is never re-sent; manifests, file tree, tests, CI and commit stats are computed by code and passed as facts.
  - Evidence selection (phase 1): a cheap lexical + skill-overlap pre-rank (bilingual synonym table) picks the top ~20 records; the LLM ranks only those.
  - Drafting checks (length, banned phrases, numbers/technologies vs evidence) are code; the reviewer LLM pass is off-switchable.
  - Every LLM call is recorded in `LLMCallLog` (purpose, model, tokens, related ids); the dashboard shows cost per company, per application and per month.
- **Cost discipline:** send extracted + trimmed text, never raw HTML. One item per call in the profile builder. Cache research and analyses so a regenerate does not re-fetch or re-send. Use the reading model by default; the draft model only where listed above.
- **Context budgets are allocated by priority, not by truncating a concatenation.** When several pages go into one prompt, use `build_company_context` (`app/companies/context.py`): each page gets a share, most decision-relevant first (job postings, then careers, then about, then home, then contact). Never `text[:N]` across pages.

---

## 9. Rules & Constraints (must follow)

- **English everywhere** except outgoing letters, form answers and generated CVs (section 1).
- **Completeness is enforced by the app,** never left to the model (section 3).
- **Preference check every company and posting.** Hard conflicts block drafting until the owner confirms; every warning cites the snippet and the page it is on.
- **Evidence-first.** No claim about a company or the owner without a verifiable source. Unknowns are explicit. The app, not the model, decides which sources, records and CV entries count; ids or URLs the model invents are dropped and shown to the owner.
- **Interview backtrack test.** Anything said about the owner (letter, form, CV) must be something he can explain in an interview without "well, what I actually meant was...". Reordering and emphasis are fine; posting terminology for adjacent-but-different work is flagged; claiming experience he lacks is never allowed.
- **Human approval before every send and every submission.** No auto-send, no auto-submit, ever; the app never fills or submits a form. The owner also approves the profile, every CV bullet and every generated CV.
- **Channels.** Email is the default channel. A site form, an ATS form or a contact form may be used when the owner picks it for that application and fills it himself (phase 6 detects them; filling is never automated). Third-party portals that require an account (kariyer.net, LinkedIn Easy Apply) are shown as links only - never automated.
- **Deliverability.** Validate the recipient; delay between sends (`SEND_DELAY_SECONDS`); never contact the same company/address twice without the explicit override (dedup in `app/applications/review.py`); stop on a bounce.
- **Attachments.** Always attach the exact CV chosen for that application, and record which file it was (`cv_snapshot`).
- **Local UI is guarded against cross-site requests.** `app/web/security.py` refuses non-localhost hosts (DNS rebinding) and any POST/PUT/PATCH/DELETE that can't prove it came from the app's own pages (`Sec-Fetch-Site`, then `Origin`/`Referer`). New routes get this automatically; never add an exemption. Tests send `Sec-Fetch-Site: same-origin` by default (`tests/conftest.py`).
- **Fetched content is sanitized before any prompt.** `app/companies/sanitize.py` strips hidden elements and invisible characters and flags instruction-shaped lines (`research.content_safety`); the `<<<UNTRUSTED>>>` fence (`app/llm/untrusted.py`) wraps it in every prompt. Keep both. This applies equally to pasted posting text - pasting by hand does not make it trusted. Never fetch URLs found inside a posting body; only the URL the owner gave.
- **No scraping of protected data.** Fetch only public pages. No logins on third-party sites except the LinkedIn rule below. Respect `robots.txt` for any recurring fetch (careers watch, ATS polling). LinkedIn job postings enter only by owner paste, or - with `LINKEDIN_RESEARCH_ENABLED` - one pasted LinkedIn job URL read through the same MCP server and throwaway account.
- **External-data providers are flag-gated.** LinkedIn research and LinkedIn posting intake (`LINKEDIN_RESEARCH_ENABLED`) and the search API (`SEARCH_API_ENABLED`, phase 3) are off by default.
- **LinkedIn company research** only under all of: `LINKEDIN_RESEARCH_ENABLED` (off by default), triggered by hand for one company at a time (never on creation, never in a bulk loop), via `mcp-server-linkedin` (stickerdaniel/linkedin-mcp-server) with a **dedicated throwaway account**, reading only - messaging and connection tools are never called. Findings are cached and re-fetched only on request.
- **Browser automation** is not built (section 12). If it ever is: headed, on the owner's machine, one application at a time, behind a flag that is off by default; it never solves or bypasses a CAPTCHA, never creates accounts, never clicks submit.
- **Search API** (phase 3) is used only to find ATS board tokens - never as the source of postings, and never to resolve a company name to a website (section 12).

---

## 10. Built So Far

The full v1 workflow is implemented end-to-end (test count in README):

- Profile Builder (CVs, GitHub per-repo, LinkedIn export, sites), coverage ledger, two-stage synthesis, owner review, optional cover-letter template.
- Preferences (hard/soft) with the research-time check; letter-language setting.
- Companies: manual + bulk add; research auto-runs (home/careers/about/contact + job pages, AI summary, preference check, LinkedIn URL from the site's own links, HR-first contact email).
- Discovery from technopark directories (`/discovery`): built-in readers for ODTÜ Teknokent, Bilkent Cyberpark, Hacettepe Teknokent, OSTİM Teknopark, Ankara Üniversitesi Teknokent, plus any directory URL; dedup against the company list (`app/companies/dedup.py::CompanyIndex`); homepage screening; owner approval.
- Profile evidence retrieval (`app/applications/evidence.py`): ~8 most relevant ProfileItems per draft.
- Drafting on the draft model, Evidence Rule enforced by the app, AI notes, CV resolution, deterministic "check before sending" (`app/applications/draft_checks.py`), reviewer pass with quote verification (`draft_review.py::quote_in_draft`).
- Review loop, single-send guarantee (conditional claim before Gmail), Gmail send with attachment, reply check, same-thread follow-ups, employee escalation.
- Optional LinkedIn company research. EN/TR UI. Dashboard with action queues.

Roadmap phases built since v1 are listed with their status in `docs/ROADMAP.md`.

---

## 11. Roadmap (summary - full specs in `docs/ROADMAP.md`)

Build in this order. Every change ships with tests against `tests/fakes.py`, additive migrations in `app/db.py`, i18n keys in both languages, and an updated README status + test count.

- **Phase 0 - Correctness fixes.** LLMCallLog; footer/obfuscated emails from raw HTML; priority-budgeted company context for drafting, research and the preference check; bounce / auto-reply classification + `BOUNCED`; preference evidence on the right page; deterministic form/ATS detection; no double send on a Gmail timeout; `cv_snapshot`; hierarchical synthesis; editable item records; schema on extraction.
- **Phase 1 - Profile depth and freshness.** Deterministic repo facts, app-planned code reading, rich record schema with verified quotes, owner interview, change detection + diffed re-analysis, per-entry CV/LinkedIn records, retrieval that uses the depth, profile write-up.
- **Phase 2 - Liveness and better discovery.** No-LLM liveness check, keyword pre-screen, CSV/XLSX import, GitHub orgs, JSON-endpoint directories.
- **Phase 3 - Job postings.** Manual intake, company matching, gated fit evaluation, legitimacy flags, careers-page watch, public ATS boards, search-based board discovery, expiry check, posting-led drafting.
- **Phase 4 - Letter quality.** Angle step, plain-text prose, style memory, fact capture, reviewer edits on the draft model, grounding audit, recipient-aware tone, app-written signature, one banned-phrase list.
- **Phase 5 - Bullet bank, tech-stack list and CV builder.** Portfolio as an owner source, approved bullets per project, evidence-backed tech list, code-assembled per-posting CV in the owner's FlowCV layout (Typst), ATS text-layer check.
- **Phase 6 - Form and posting detection.** Forms/ATS/postings detected on company sites, application channel tracking. Form filling is not automated (owner decision).
- **Phase 7 - Outcomes and analytics.** Funnel page, outcome archive.

---

## 12. Deferred / Never

Deferred (do NOT build without the owner asking):
- Automatic name -> website resolution (search-based) with owner confirmation.
- Contact/email discovery via Hunter.io / Apollo (keep `Contact` source-agnostic; gate any provider behind a flag).
- LLM auto-suggesting new companies.
- Scheduled background jobs (careers watch, reply checks) - everything stays owner-triggered for now.
- Interview prep (story bank of STAR examples built from the profile) and salary benchmarking.
- Form answers and browser fill assist (Playwright) - the owner chose detection only (phase 6).
- Fully automatic follow-ups.

Never:
- Auto-send, auto-submit, or clicking a submit button.
- Automating kariyer.net, LinkedIn Easy Apply or any portal that needs the owner's account.
- CAPTCHA solving or bypassing bot protection.
- Hidden keywords or any content in a CV or letter that the owner hasn't approved.

---

## 13. Stack & Conventions

- **Backend:** Python, FastAPI + Jinja2, SQLAlchemy on SQLite (`data/app.db`). Schema changes are additive `ALTER TABLE` migrations in `app/db.py`; existing data is never rebuilt.
- **Fetching:** `httpx` with `BROWSER_HEADERS`, readability + BeautifulSoup, sanitizer before any prompt. No browser automation.
- **Email:** Gmail API via OAuth2 (`app/email_send/gmail_client.py`). The OAuth app must be "In production" or the token expires after 7 days.
- **PDF:** Typst (the `typst` PyPI package) for generated CVs, Lato fonts bundled under `app/cv_builder/fonts/`; `pypdf` for text-layer checks.
- **Config:** everything through `app/config.py::Settings`; new settings go into `.env.example` with a comment.
- **Tests:** `python -m pytest tests/`. LLM via `FakeLLMProvider` / `SequencedLLMProvider`; Gmail, LinkedIn, the search API and HTTP are faked at their module seams. The suite never touches `data/app.db` or the owner's `.env`.
- **Layout:** follow the existing packages (`app/companies`, `app/discovery`, `app/applications`, `app/profile_builder`, `app/email_send`, `app/web`). New packages: `app/postings/` (phase 3), `app/cv_builder/` (phase 5), `app/channels/` (phase 0 detection, phase 6).
