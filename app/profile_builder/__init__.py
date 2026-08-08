"""
One-time Profile Builder (CLAUDE.md section 3). NOT YET IMPLEMENTED.

Planned modules (one source type each, all writing ProfileItem rows):
- cv_parser.py        parse uploaded PDF/docx CVs (pypdf / python-docx)
- github_ingest.py     list all repos via PyGithub, owner confirms include/exclude,
                        pull README/description/topics/languages/tree/key files per repo
- linkedin_ingest.py   parse an uploaded LinkedIn "Save to PDF" export (no scraping)
- site_ingest.py       fetch owner's extra sites/portfolio, extract main text
- coverage.py          the coverage ledger: every source item -> analyzed/skipped/needs_review
- synthesis.py         stage 2: synthesize Profile.summary/skills/target_roles from
                        the collected ProfileItem records, each claim source-tagged

Core principle to preserve: the app enumerates items deterministically and processes
one item per LLM call - the model never decides which items to look at.
"""
