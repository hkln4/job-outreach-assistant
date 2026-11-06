# Roadmap (build in this order)

Detailed specs for the phases summarized in CLAUDE.md section 11. Work on the earliest
unfinished phase unless the owner asks for something else; the status line under each
phase heading is kept current.

Each item lists what to change and how to know it is done. Every change ships with tests
against `tests/fakes.py` (no real LLM, Gmail, LinkedIn, search API or browser in `pytest`),
additive migrations in `app/db.py`, i18n keys in both languages, and an updated README
status + test count.

---

## Phase 0 - Correctness fixes

Status: done.

**LLMCallLog (cross-cutting, first).** Every LLM call site records `purpose`, `model`,
input/output tokens and the related `company_id` / `application_id` / `profile_item_id`
in an `LLMCallLog` table, so the cost of every later phase is measurable. The dashboard
shows cost per company, per application and per month. Logging wraps the provider
(`app/llm/logging.py`), so business logic only names the purpose.

**Bug 1 - Emails and forms in footers are lost.** `_readable_text`
(`app/companies/fetch.py`) returns readability's main article when it is >= 400 chars,
which discards the footer - where contact emails usually are. `get_text` also drops
`mailto:` hrefs, and Cloudflare-protected addresses show as "[email protected]".
- `extract_emails(html, url) -> list[{email, context, url}]` works on the **raw HTML** of
  every fetched page: `mailto:` hrefs (anchor text as context), `data-cfemail`
  (`_decode_cfemail` moved into a shared module), and plain-text addresses including
  common obfuscations (`[at]`, `(at)`, ` at ` + `dot`) with a conservative regex.
- Merged with the LLM's emails (deterministic ones win; deduped case-insensitively), then
  `classify_email` / `pick_contact_email` as before.
- A short, capped "footer/contact block" (text of `<footer>`, `<address>`, elements whose
  class/id contains `footer|contact|iletisim`) is appended to each page's extracted text.
- Done when: tests cover a long page whose only email is a footer `mailto:`, a
  `data-cfemail` address, and an `[at]` address - all end up in `research.emails`.

**Bug 2 - Job postings are cut from every multi-page prompt.** Three call sites join all
pages and slice the result: drafting (`[:24000]`), the preference check
(`app/preferences/checker.py`, `[:24000]`) and research itself (`[:40000]`, which 4 core
pages + 3 job pages at 6000 chars just exceed). Job pages are appended last, so they are
the part that gets cut - the most useful text for a letter, and a likely place for a
dealbreaker ("savunma projelerinde görev alacak").
- `build_company_context(research_or_pages, budget)` orders pages job postings -> careers
  -> about -> home -> contact, gives each a share (unused share flows to the next page),
  and for drafting prepends `research.summary` and `research.open_positions`.
- Used by all three call sites (and the reviewer pass).
- Done when: a test with 4 core pages + 2 job pages asserts both job pages' text reaches
  the drafting prompt and the preference-check prompt.

**Bug 3 - Bounces and auto-replies count as replies.** `thread_has_reply` returned True
for any message not from the owner (case-sensitive substring), and never returned True
when `GMAIL_SENDER_ADDRESS` was empty.
- `classify_thread(thread_id) -> {"kind": "reply" | "bounce" | "auto_reply" | "none", detail}`
  reads `From`, `Subject`, `Auto-Submitted`, `X-Autoreply`, `X-Autorespond`, `Precedence`,
  `X-Failed-Recipients`, `Content-Type`. Bounce: sender local part `mailer-daemon` /
  `postmaster`, `X-Failed-Recipients`, or `multipart/report; report-type=delivery-status`.
  Auto-reply: `Auto-Submitted` other than `no`, the X-Auto* headers,
  `Precedence: auto_reply|bulk|junk`, or subjects starting with "Automatic reply",
  "Auto:", "Out of Office", "Otomatik yanıt", "Otomatik cevap", "Ofis dışında". Senders
  are compared by parsed address, case-insensitively; the owner's own address comes from
  `GMAIL_SENDER_ADDRESS`, or Gmail's `SENT` label when it is unset.
- `ApplicationState.BOUNCED`; on bounce the address is recorded as invalid on the company
  (`Company.invalid_emails`) / contact, and the application page offers the other known
  emails. Auto-replies become a note on the application.
- Replies outside the thread: Gmail search `from:(@<company domain>) after:<sent date>`;
  results are shown as "possible reply - confirm", never auto-applied.
- Done when: tests cover a bounce, an out-of-office, a real reply and a mixed-case owner
  address.

**Bug 4 - Preference evidence points at the homepage.** Each flag's snippet is located in
the page texts (whitespace-normalized, casefolded substring); that page's URL is used; if
not found, the first page is kept and `source_url_verified: false` is shown in the UI.
Suggested contacts get the same treatment (the page their name is on).

**Bug 5 - `application_form` is a guess.** `detect_channels(html, url)` (in
`app/channels/detect.py`) works on raw HTML: `<form>` with `input[type=file]` or fields
named/labelled cv, resume, özgeçmiş, başvuru; links or iframes to known ATS hosts. Results
go to `research.channels_detected` with the matching snippet. The LLM field stays only as
a fallback note. ATS host classification matches the registrable domain exactly or as a
subdomain (so `evil-greenhouse.io` or `greenhouse.io.example.com` don't count). Host list:
kolayik.com, workable.com, lever.co, greenhouse.io, ashbyhq.com, smartrecruiters.com,
teamtailor.com, personio.de / personio.com, bamboohr.com, recruitee.com, breezy.hr,
jotform.com, docs.google.com/forms + forms.gle, typeform.com, hsforms.com / hubspot forms;
kariyer.net and linkedin.com/jobs as account-only portals (link only).

**Bug 6 - Possible double send on a Gmail timeout.** `approve_and_send` reverted to
`PENDING_APPROVAL` on any exception, but a timeout can happen after Gmail accepted the
message. We set our own `Message-ID` header before sending and store it; on an exception
the Sent folder is searched for `rfc822msgid:<id>`, and as a second check for
`in:sent to:<recipient> subject:"<subject>" after:<date>`; if either finds it, the send
is recorded instead of reverted.
- **Verify with one real send:** Gmail is not guaranteed to keep a client-supplied
  `Message-ID`. If it rewrites it, the subject/recipient search is what catches the case.

**Bug 7 - Which CV file was sent isn't recorded.** `Application.cv_snapshot`
(`path`, `sha256`, display name) is stored at send time; the file is copied into
`data/sent_cvs/` so replacing a CV variant later can't rewrite history.

**Bug 8 - Synthesis silently drops records.** `synthesize_profile` sent
`records_text[:20000]`. Synthesis is now hierarchical when the records exceed the budget
(group by source type, split groups into chunks, synthesize each, then merge), and a test
asserts that every analyzed item's `source_ref` reaches some synthesis call.

**Bug 9 - Item records can't be edited.** Edit form per ProfileItem (every field of the
record schema - the four v1 fields until phase 1 lands, then the phase 1 schema). Owner
edits are recorded in `owner_edited_fields` and survive re-analysis (re-analysis proposes
changes to those fields as a diff instead of overwriting them).

**Bug 10 - Extraction has no schema.** `extract_item` passes its Pydantic schema like the
other call sites.

---

## Phase 1 - Profile depth and freshness

Status: done.

Why first: every later phase draws on these records. A v1 repo record is
`{purpose, stack, role, highlights}`; evidence selection sees only `purpose[:160]` and six
stack items, and the draft sees at most 1,500 characters of it. A detailed README meant no
code was read at all; commit history was the last five messages.

**1. Deterministic repo facts (code, no LLM)**, stored as `ProfileItem.facts`:
- Manifests parsed for real dependencies: `requirements*.txt`, `pyproject.toml`,
  `setup.cfg`, `environment.yml`, `package.json`, `go.mod`, `Cargo.toml`, `pom.xml` /
  `build.gradle`, `Dockerfile`, `docker-compose*.yml`. The stack list is built from these
  (mapped to display names, e.g. `torch` -> PyTorch) and **the model may not add a
  technology that is not in the manifests, imports or README** (enforced by code).
- Structure: languages by bytes, top-level directories, file counts by type, notebooks,
  tests present (+ count), CI workflows, deployment files, license, packaged name,
  homepage/demo URL.
- Activity: first and last commit date, total commits, the owner's share of commits,
  contributors count, releases/tags, stars/forks.
- README claims cross-check: features the README names (Docker, CI, tests, API,
  deployment) vs files that exist; mismatches become `claims_to_verify`.

**2. Code reading the app plans, the model only ranks.** The app builds the file tree
(skipping vendored/generated paths), always includes entry points, manifests and the
README, then offers the reading model the remaining **paths only** to rank. The app reads
the top files within `PROFILE_REPO_CHAR_BUDGET` (default 30000). Large repos are summarized
per top-level module first (map), then combined (reduce). A detailed README only lowers
the code budget.

**3. Richer record schema** (written by the **draft model**, one-time per repo):
```
one_liner, problem, approach, architecture[], stack_used_for[{tech, for}],
owner_contribution, results[{claim, quote, source: readme|code|owner}],
maturity{tests, ci, deployed, packaged, notes}, domains[],
role_angles{ml_ai, computer_vision, backend_data, mlops}, talking_points[],
claims_to_verify[], open_questions[]
```
Every `results[].quote` must be found verbatim in the text sent (checked by code); unmatched
results move to `claims_to_verify`. `maturity.tests/ci/deployed/packaged` come from facts,
not from the model. v1 fields (`purpose`, `stack`, `role`, `highlights`) are derived from
the rich record so older readers keep working.

**4. Owner interview per project.** `open_questions` (max 5) are shown on the item page;
answers are stored as `owner_facts` (`source: "owner"`) and count as evidence. Unanswered
questions never block anything.

**5. Change detection and re-analysis.** `source_version = {pushed_at, head_sha,
analyzed_at}`. "Check for changes" (owner-triggered, no LLM) lists repos whose HEAD moved,
plus new and deleted repos. Re-analysis sends the old record + commit messages and changed
files since `head_sha` (compare API) and asks for an updated record, shown as a diff before
saving; owner-edited fields and owner facts are kept. Re-synthesis is offered, not run.

**6. Same depth for other sources.** CV and LinkedIn records split into one record per
role / project / education entry (`parent_ref` = the source file), with dates, employer,
title and bullet text kept verbatim; site pages keep project sub-pages as separate items.

**7. Retrieval uses the depth.** `_index_line` includes `one_liner`, `domains` and the
deterministic stack; a lexical + skill-overlap pre-rank (bilingual synonym table) picks
the top ~20 records before the LLM ranks; the drafting prompt receives the role-relevant
`role_angles` entry, `results` and `talking_points` instead of a truncated JSON dump.

**8. Profile write-up.** After analysis the draft model writes a long, readable "about me"
document from all records (projects, experience, skills - every claim with its source
record). The owner reads and edits it on the Profile page; it is the base for project
selection and bullets (phase 5), and is never sent anywhere itself.

Done when: a fixture repo with a manifest, tests and CI produces a record whose stack
equals the manifest-derived list, `maturity.tests/ci` are true from code, a fabricated
result without a quote is moved to `claims_to_verify`, and an unchanged repo makes zero
LLM calls on "check for changes".

---

## Phase 2 - Liveness and better discovery

Status: done.

- `app/discovery/liveness.py::check_liveness(url) -> {status: ALIVE | STALE | DEAD | UNKNOWN,
  signals[{name, value, evidence, url}], checked_at}`. No LLM. Signals: DNS, HTTP status,
  TLS validity, final URL after redirects (different registrable domain = possibly
  acquired/renamed), parked/placeholder patterns ("domain for sale", "bu alan adı
  satılıktır", "yapım aşamasında", "under construction", "coming soon", default server
  pages), freshness (footer copyright year, sitemap `lastmod`, `Last-Modified`), content
  length, careers page present.
- Status rules: DEAD = no DNS / connection failure / 4xx-5xx on home / parked; STALE =
  newest freshness signal older than `LIVENESS_STALE_YEARS` (default 2); ALIVE otherwise;
  UNKNOWN when no freshness signal exists.
- Bulk run ("check all pending"), owner-triggered, concurrency cap 8, one request at a time
  per host. Cached on the candidate/company. Screening skips DEAD by default (owner can
  force). Discover table: liveness column and filter.
- Registrable-domain comparisons use `app/channels/domains.py` (a small built-in list of
  multi-label public suffixes such as `com.tr`, `gov.tr`, `co.uk` - no network fetch of
  the public suffix list).
- Preference **keyword pre-screen** on homepage text (`Preference.signals`) before LLM
  screening; a hit is a flag, never a decision.
- New discovery sources (owner-triggered, same candidate -> approval path): generic CSV/XLSX
  import with column mapping; GitHub organizations (`type:org location:<city>`, most recent
  `pushed_at` as activity signal, `blog` as website); JS-only directories via an
  owner-supplied JSON endpoint (generic JSON reader with field mapping).
- Done when: liveness tests cover a parked page, a JS redirect shell, a sitemap with old
  `lastmod`, and a dead host; the Discover page filters by status.

---

## Phase 3 - Job postings (posting-first entry point)

Status: done.

- **Manual intake:** "Add posting" takes a URL or pasted text. URL -> app fetch (sanitized,
  same fetcher); text -> sanitized as untrusted. The reading model parses it into
  `JobPosting` fields. `source = linkedin` only labels a posting the owner pasted from
  LinkedIn - the app never fetches LinkedIn job pages.
- **Company matching** via `CompanyIndex`; unmatched -> proposed new Company, owner approves.
- **Fit evaluation** (reading model, evidence-quoted): deterministic gates first (language,
  location / remote eligibility, seniority by regex where explicit), then hard preferences,
  then 0-100 scores for technical / experience / domain / career alignment, each with the
  posting quote and profile record, plus strengths and gaps. Quotes not found in the posting
  are dropped. Verdict shown; never auto-dismissed.
- **Legitimacy flags** (never affect the score): no company identifiable, posting older than
  `POSTING_STALE_DAYS`, reposted with the same `content_hash`, off-domain apply link.
- **Research skip:** re-research of an already-researched company is skipped unless the
  owner asks (button) or the site's content hash changed.
- **Careers-page watch:** owner clicks "check careers pages" for `APPLICABLE` companies;
  re-fetch, compare hash of extracted text, only on change run the reading model to extract
  postings -> dashboard queue "new postings". `robots.txt` respected.
- **Public ATS boards:** Greenhouse, Lever, Ashby, SmartRecruiters public JSON endpoints read
  when a board is detected (verify each endpoint's shape before relying on it in production;
  parsers are defensive). Others are read as HTML careers pages.
- **ATS board discovery (search -> board -> API):** `SEARCH_API_ENABLED` + `SEARCH_API_KEY`
  (Tavily by default, Brave as an alternative). A search only finds board tokens; postings are always read fresh from the
  board API. Remote postings get an eligibility gate ("Remote - US", "US only", "EU only",
  "must be authorized to work in ...").
- **Turkey location filter (code):** board APIs return a location per posting.
  Accept: Turkey, Türkiye, Istanbul, İstanbul, Ankara, Izmir, İzmir, "Remote - Turkey".
  Ambiguous, shown to the owner: EMEA, Europe, bare "Remote", Anywhere.
  Reject: "US only", "Remote - US", "EU only", "must be authorized to work in ...".
  Search queries (`site:jobs.lever.co Istanbul`, `site:jobs.ashbyhq.com Turkey`,
  `site:boards.greenhouse.io Türkiye`) and aggregators (Jobgether, Built In) are used only
  to find board slugs; postings always come fresh from the board API.
- **LinkedIn job postings:** with `LINKEDIN_RESEARCH_ENABLED`, a pasted LinkedIn job URL is
  read through the same MCP server and throwaway account as company research (one posting,
  owner-triggered, read-only). Without it, only pasted text is accepted.
- **Expiry check:** before drafting, re-fetch the posting URL; 404 / "ilan yayından
  kaldırıldı" / "position closed" -> `EXPIRED` with a warning.
- Drafting for a posting: the posting text leads the company context; `target_position` =
  posting title; `Application.job_posting_id` set; letter language follows the posting's
  language when the owner has no preference.

---

## Phase 4 - Letter quality

Status: done.

1. **Angle step.** One draft-model call proposes 2-3 angles `{company_hook (exact quote),
   hook_url, owner_evidence_id, bridge, position}`; the app verifies each quote is in that
   page's text and each evidence id was offered. Owner picks one on the review page;
   "regenerate with another angle" is one click.
2. **Prose as plain text.** The writing call returns `Subject: ...` on the first line and
   the body after it. Metadata (sources, evidence ids, CV choice, reasoning) comes from the
   angle step and a small structured follow-up call.
3. **Style memory.** Approved letters can be marked "good example"; the two most similar
   (same language, same CV `target_role`) are passed as examples. Owner edits before
   approval store a diff. Reject feedback can be promoted to a `StyleNote`. Style page.
4. **Fact capture.** Feedback or an edit that states a fact about the owner proposes a
   `ProfileFact`; once approved it is a valid evidence record.
5. **Reviewer returns edits.** Each issue is `{quote, replacement, reason}`; edits are
   applied individually. The reviewer runs on the draft model. Quote filter kept.
6. **Deterministic grounding audit.** Every number, year and technology name in the letter
   must appear in the evidence records, profile or company text; misses are listed in
   "check before sending".
7. **Recipient-aware tone.** HR address / technical contact / founder of a small company.
8. **Signature block** appended by the app from `OwnerSettings` (name, phone, GitHub,
   portfolio, LinkedIn) - never written by the model.
10. **Portfolio links.** If the letter's lead project has a page on the owner's portfolio
    (an ingested portfolio sub-page linked to that project), the app links that page in the
    letter. The portfolio is an owner-side source only; it plays no part in company research.
9. **One banned-phrase list** (`app/applications/phrases.py`) shared by the prompt and
   `draft_checks.py`, extended with Turkish fillers.

---

## Phase 5 - Bullet bank, tech-stack list and CV builder

Status: done (`/cv-builder`). Typst template `app/cv_builder/templates/cv.typ`, Lato in
`app/cv_builder/fonts/` (OFL). Kerning is off in the template: with it on, pypdf extracted
"H asan K alin"; words with a hyphen are boxed so "mid-LTV" never breaks. The consistency
check (`app/cv_builder/consistency.py`) compares metric numbers per project across record,
bullets, portfolio and CV.

No CV is written per application. Bullets are written once per project into a bank; each
CV is assembled from the bank, mostly by code.

**Portfolio (owner source).** `OwnerSettings.portfolio_url`; ingested once through site
ingest (project sub-pages as separate items, `source: owner` evidence). A consistency check
flags contradictions between portfolio, CV, profile records and bullet bank (e.g. different
numbers for the same metric).

**Bullet bank.**
- Projects: the app suggests strong projects from the profile records; the owner selects /
  deselects, and his choice always wins.
- Per selected project, 3-6 ATS-friendly bullets: action verb + what was built + key tech +
  result. Each bullet stores tags (tech, domain, role type: ml_ai / computer_vision /
  backend_data / mlops), its source (a `results[].quote` or an owner fact) and status
  `draft` / `approved`. Only approved bullets reach a CV.
- Numbers: a real metric with its source; else a concrete scale fact (dataset size, rule
  count, test count, languages supported); else no number and the app asks the owner for a
  metric. A number not found in the bullet's source is flagged by code - never invented.
- Every bullet is editable. Edited bullets are locked; when a repo changes, new suggestions
  arrive as a diff and never overwrite the owner's text.

**Tech-stack list.**
- Sources: repo manifests/imports (code), Professional Experience bullets from the CV
  (technologies that appear only there), and manual additions.
- Each technology shows where it is used. Manual additions without evidence are allowed but
  marked "no evidence".
- Groups follow the current CV: Programming Languages, Frameworks & Tools, Technologies,
  Generative AI. A technology whose only evidence was removed is flagged before it can
  appear on a CV.

**Per-posting CV (mostly code).**
- Posting keywords matched to the tech list with the synonym table; matched items first,
  irrelevant groups trimmed to fit one page.
- Projects ranked by tag overlap with the posting; top 3. The LLM is optional, only for
  genuine ties.
- Keyword report: covered vs missing. Missing keywords are gaps, never added. No hidden text.
- Preview before rendering: add/remove technologies, swap projects, reorder, edit bullets.
  Edits apply to this CV only unless the owner chooses "save to bank".
- Open applications (no posting): the role type from company research replaces posting
  keywords; or the owner picks a saved variant.

**Layout** (the owner's current FlowCV CV, re-created as a template).
- Lato regular/bold/italic; centered name + "Computer Engineer"; one contact row (email,
  LinkedIn, GitHub, portfolio, phone, city) with small icons; section titles with a
  full-width rule; dates right-aligned; compact bullets; one page.
- Order: Education -> Professional Experience -> Technical Skills -> Projects ->
  Certificates (optional).
- Education and Professional Experience are locked: copied verbatim, never re-selected or
  reworded.
- Certificates: one line, names and issuers only; off by default; on when the posting asks
  for certifications or the owner enables it.
- Hyphenation off, compound words kept on one line (the FlowCV text layer turns "mid-LTV"
  into "midLTV").
- The owner approves the template once, side by side with the FlowCV PDF.
- Rendering: Typst (`typst` PyPI package, no system install). ATS check with `pypdf`:
  name/email/phone present and in order, headings found, keyword report matches the
  extracted text, and any word that differs between source and text layer is flagged. A
  failing check blocks that CV, not the letter.
- The approved PDF is stored (`GeneratedCV` with sha256) and attached to the application.

---

## Phase 6 - Form and posting detection

Status: done.

Owner decision (2026-11): detecting forms and postings on company sites matters now;
automating form filling does not. No Playwright, no fill assist.

- Detection from phase 0 bug 5 (forms, ATS boards, portals, with form schema: labels, names,
  types, required, maxlength, options, file inputs, CAPTCHA).
- Job postings found on a company's own pages (job links, ATS boards) become `JobPosting`
  records (phase 3) from the company page, one click.
- `Application.channel` records how the owner applied (`email` | `site_form` | `ats_form` |
  `contact_form`); a form application is marked submitted by the owner and then tracked
  with the domain reply search from phase 0 bug 3.
- Form answers and browser fill assist are deferred (section 12).

---

## Phase 7 - Outcomes and analytics

Status: done (`/funnel`, "Record outcome" on the application page).

- Funnel page: applications by source, channel, CV variant, language, angle type -> reply /
  interview rates and median time to reply. Plain HTML + inline SVG.
- "Outcome" action records the result and archives the posting, letter, CV and answers.

---

## After the roadmap - hallucination guards

Status: done.

- Sentence-level evidence for letters (`app/applications/claims.py`): the metadata call maps
  each sentence to a source (page URL or record E-number) and a quote; code verifies the quote
  in that source, requires a page quote for company claims and a record quote for owner claims,
  re-checks "nothing to verify" sentences that look factual, and lists unsupported sentences in
  "check before sending". Edits mark the check stale; "re-check" re-runs it.
- No reuse of unverified model text: the research summary reaches prompts only as
  quote-verified sentences (`research.summary_verified`), open positions only when their title is
  on a page; repo records split into citable facts and labelled model framing (role angles and
  talking points filtered by code); claim checks accept quotes only from citable fields
  (`app/applications/evidence.py::citable_text`).
- Not yet: a measured hallucination rate (an eval set of real samples), profile-synthesis
  `evidence_source` validation, a wider grounding audit (employers, durations, role claims).
