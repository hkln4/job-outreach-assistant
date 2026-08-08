# CLAUDE.md

Project guide for the coding agent. Read this fully before working on the project.

---

## 1. Project Overview

- **Purpose:** A personal tool that automates and standardizes job-application outreach to tech companies (primarily Ankara). It turns a curated list of target companies into tailored, human-approved application emails, tracks every application, and escalates to individual employees when a company does not respond.
- **Primary goal:** actual personal use. Secondary: portfolio project. When trade-offs arise, favor a working, reliable tool over architectural showmanship - but keep it clean enough to show.
- **Single user** (the owner). No multi-tenant auth or account system is needed.
- **Language:** everything is in **English** - code, identifiers, comments, UI, generated cover letters, and all stored content.
- A one-time **Profile Builder** (section 3) produces a durable, detailed profile of the owner. A **Preferences** layer (section 4) warns about companies that conflict with the owner's constraints. Both feed the per-company workflow.

---

## 2. Shape of the System

A normal application, not an agent-in-a-terminal:

- **Backend** owns all logic and state, makes the LLM calls, fetches web pages, and sends email.
- **Review UI** (minimal, local, localhost only) is where the owner reads drafts, approves/rejects, and manages the company list, profile, and preferences.
- **LLM** is called through the backend via a **provider-agnostic interface** (see section 8).

The owner drives it through the UI; the backend does the work. There is no autonomous background service - actions happen when the owner triggers them, and every send is gated by explicit approval.

---

## 3. One-Time Profile Builder (Onboarding)

Goal: build one durable, detailed, reviewed **profile of the owner**, used as context in every cover letter. Run once at setup; re-runnable whenever new work is added.

**Core principle: completeness is the app's job, not the model's.** The app enumerates discrete items from each source and processes them one at a time. The model never decides which items to look at - that is what guarantees every project is examined rather than skimmed.

### Sources & ingestion
- **Uploaded CVs** (PDF/docx): parse each file fully; each CV is one item.
- **GitHub** (official API): list ALL repos deterministically. Pre-filter forks/empty repos, then the owner confirms include/exclude (not every repo is a real project). For each included repo, pull README, description, topics, languages, file tree + selected key source files, stars, and recent commits.
- **LinkedIn**: the owner uploads a profile **PDF export** ("Save to PDF") or data archive; parsed as a document. No LinkedIn scraping.
- **Extra sites** (portfolio, blog, personal site): fetch each URL, extract main text; optionally follow project sub-pages with a depth cap.

### Per-item deep pass
- Process **one item per LLM call** (or small batches) so each gets full attention and context is not diluted.
- Force **structured output** per item: purpose, stack, owner's role, highlights, source links. Unknown fields must be explicit ("unknown"), never hand-waved or invented.
- Evidence-first: record only what the source contains; flag uncertain items as `needs_review`. Never invent skills or tech.

### Coverage ledger
- Keep a manifest of every item found and its status (`analyzed` / `skipped` / `needs_review`). The owner can verify coverage at a glance (e.g. "42 repos found, 42 analyzed") - nothing is silently dropped.

### Two-stage synthesis
- Stage 1: per-item extraction (above).
- Stage 2: synthesize the overall profile (summary, skills-with-evidence, projects, experience, education, target roles) **from the collected item records**, each claim tagged with its source. The profile is built from N thorough analyses, not one glance.

### Review
- The owner reviews/edits the profile and per-item records before they are saved as the master profile. The owner is the final authority on facts about himself.

### Depth caveat
- Depth is bounded by what a source contains. Repos with good READMEs analyze richly; bare repos yield thin records - the app then samples key source files, or flags the repo so the owner can add a short description.

---

## 4. Preferences & Constraints (owner)

The owner keeps a set of preferences the system checks each company against, so it can warn **before** drafting.

Two tiers:
- **Hard exclusions (dealbreakers)** - e.g. defense industry. Default behavior: a match **blocks drafting** and raises a prominent warning until the owner confirms. (This default is a knob; it can be set to warn-only.)
- **Soft preferences** - e.g. prefer remote, avoid pure consultancy. A match is surfaced as a note, not a block.

How the check works:
- During research (workflow step 2), the backend passes the extracted company text + the preferences to the LLM and asks whether the company appears to conflict with any of them.
- Output is **evidence-first**: each flagged preference returns the on-site snippet + source URL that triggered it, plus severity and confidence. It is a **flag for the owner's judgment, not an automatic verdict** - website text can mislead (a civilian company may mention a defense client; a defense firm may not say "defense" plainly), so the owner sees the evidence and decides.

Scope note: preferences cover things to **avoid or require**. Positive role targeting (ML / CV / backend) is already handled by the profile's `target_roles`; do not duplicate it here.

---

## 5. End-to-End Workflow (per company)

1. **Company list (manually curated).** The owner adds target companies and vets them manually (including a manual LinkedIn check for size / whether the company is alive / who to contact). Each company carries a `state`; the system only acts on companies marked `APPLICABLE`.
2. **Resolve site + fetch + preference check.** Each company's **website URL is provided by the owner** when adding it (name -> URL is not guessed by the backend in v1). The **backend then fetches** that site + the careers page, extracts main text (not raw HTML), trims it, and passes that text to the LLM. Run the **preference check** (section 4) here: if a hard exclusion matches, warn and **block drafting** until the owner confirms. The LLM also reports, as *feedback only*, whether the company uses an application form / LinkedIn / kariyer.net. Retrieval stays in the app - do **not** use the model's own web-search tool. **Email is always the send channel.**
3. **Recommendation.** Using the owner's **profile** (section 3) plus the company research, the LLM recommends (a) which open position to target and (b) which CV variant to use, with explicit written reasoning.
4. **Draft.** The LLM writes a company-specific cover-letter email grounded in the profile. **Every company-specific claim must be backed by a source URL** (Evidence Rule). Sources + any preference warnings are shown in the review UI.
5. **Review (human-in-the-loop).**
   - **Approve** -> send via the Gmail API with the correct CV attached.
   - **Reject** -> owner enters free-text feedback -> the LLM regenerates the draft. Loop until approved.
6. **Track.** The application is recorded and moves through its state lifecycle. **Past applications are retained with their final state.**
7. **Escalate (employee fallback).** If the company's generic address does not reply within a threshold, the owner can send a tailored email to specific employees at that company. In v1, employee contacts are **entered manually** (see Deferred).

---

## 6. Entities / Data Model (suggested)

**Profile** - `id`, `summary`, `skills[]` (each: skill + evidence source), `target_roles[]`, `updated_at`

**ProfileItem** (one per source item: repo / CV / page / LinkedIn) - `id`, `source_type`, `source_ref`, `extracted` (structured: purpose, stack, role, highlights), `status` (`analyzed` / `skipped` / `needs_review`), `source_urls[]`

**Preference** - `id`, `description`, `type` (`hard` | `soft`), `signals[]` (optional keywords/notes to aid matching)

**CV** - `id`, `name`, `target_role`, `file_path`, `notes`

**Company** - `id`, `name`, `website_url` (owner-provided; optional for companies with no site), `domain`, `careers_url`, `contact_email`, `state`, `other_channels`, `research` (cached extracted text + findings, each with source URL), `preference_check[]` (each: `preference_id`, `matched`, `severity`, `evidence_snippet`, `source_url`, `confidence`), `notes`, `created_at`

**Application** - `id`, `company_id`, `cv_id`, `target_position`, `subject`, `cover_letter_body`, `sources[]`, `state`, `sent_at`, `last_action_at`, `gmail_message_id` / `gmail_thread_id`, `revision_history[]`

**Contact (employee)** - `id`, `company_id`, `name`, `role`, `email`, `source_url`, `added_manually` (bool), `state`, `application_id`

---

## 7. State Machines

**Company.state** (owner's manual vetting): `APPLICABLE` / `ON_HOLD` / `REJECTED` -> the system drafts only for `APPLICABLE`.

**Application.state**: `DRAFT` -> `PENDING_APPROVAL` -> (`REVISION` <-> `PENDING_APPROVAL`) -> `SENT` -> `AWAITING_REPLY` -> one of { `REPLIED`, `INTERVIEW`, `REJECTED`, `NO_RESPONSE` }

**ProfileItem.status**: `analyzed` / `skipped` / `needs_review`

**Employee-escalation** reuses the application lifecycle, triggered when the parent company application is `NO_RESPONSE` past the threshold.

---

## 8. LLM Layer (provider-agnostic)

- All model calls go through **one internal interface** (e.g. `LLMProvider.generate(...)`). No provider SDK is called directly from business logic.
- **Default provider: Gemini** (a Flash-class model is enough for reading page text, per-repo analysis, preference checks, and writing cover letters). Key from Google AI Studio, covered by the owner's Google AI Pro subscription; keep low-volume usage inside the subscription/free quota.
- **Claude provider is implemented but inactive.** `ANTHROPIC_API_KEY` is left **empty** in `.env`. When funded later, set the key and flip `LLM_PROVIDER=claude` - no other code changes.
- **Config** (`.env`): `LLM_PROVIDER` (gemini|claude), `GEMINI_API_KEY`, `ANTHROPIC_API_KEY` (empty for now), plus model names per provider.
- **Cost discipline:** the biggest lever is how much text is sent. Always send extracted + trimmed text, never raw HTML. Process source items one at a time (profile builder) with trimmed inputs. Cache extracted research/analysis so a regenerate does not re-fetch or re-send.

---

## 9. Rules & Constraints (must follow)

- **English everywhere.**
- **Completeness is enforced by the app,** never left to the model: enumerate items deterministically and track them in a coverage ledger (section 3).
- **Preference check every company** during research. Hard-exclusion conflicts block drafting until the owner confirms; every warning cites on-site evidence (snippet + URL).
- **Evidence-first.** No claim - about a company or the owner - without a verifiable source. Surface sources at review. Unknowns must be explicit. If it cannot be verified, do not claim it.
- **Human approval before every send.** No auto-send, ever. The owner also reviews the generated profile before it is saved.
- **Email is the definitive channel.** Other channels are informational feedback only.
- **Deliverability.** Validate the recipient address before sending; add a delay between sends; never send to the same company/contact twice (dedup).
- **Attachments.** Always attach the exact CV variant chosen for that application.
- **No scraping of protected data.** Fetch only public pages the owner is entitled to read. LinkedIn is ingested only via the owner's uploaded export.

---

## 10. MVP Scope (v1 - build this first)

- One-time Profile Builder: CVs + GitHub (per-repo) + LinkedIn PDF + extra sites, with coverage ledger and owner review.
- Preferences layer (hard/soft) with the research-time check and warnings.
- Manual company list with states; owner-provided website URLs.
- Backend fetch of site + careers page, text extraction, sourced cover letter + CV pick + reasoning, grounded in the profile.
- Review / approve / reject-with-feedback loop.
- Gmail API send with the correct CV attachment.
- Application lifecycle tracking, including retained history of past applications.
- Employee fallback with **manually provided** employee contacts.
- Provider-agnostic LLM layer with Gemini active and Claude wired but inactive.

---

## 11. Deferred / Backup (v2 - do NOT build in v1)

- Automatic name -> website resolution (search-based) with owner confirmation.
- Automated contact/email discovery + company enrichment via Hunter.io (or Apollo).
- Automated LinkedIn research (headcount, roles, key contacts).
- LLM auto-suggesting new companies to add (with owner approval).
- Follow-up automation and analytics / A-B testing of CV variants.

Design v1 so these slot in later: keep `website_url`, `contact_email`, and `Contact` records source-agnostic, and gate any external-data provider behind a flag.

---

## 12. Suggested Stack (flexible - owner decides)

- **Backend:** Python (owner's background: backend / data engineering).
- **Storage:** SQLite is sufficient for a single user.
- **Parsing:** PDF/docx text extraction (for CVs and the LinkedIn export).
- **GitHub:** official REST API client.
- **Web fetch:** HTTP client + a readability/main-text extractor; cap extracted text length.
- **Email:** Gmail API via OAuth2 (google-api-python-client) from the backend.
- **Review UI:** a minimal local web app.
- **LLM:** provider-agnostic layer; Gemini (Google AI Studio) default, Claude (Anthropic API) as a config-flip alternative.