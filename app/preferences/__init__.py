"""
Preferences & Constraints checker (CLAUDE.md section 4). NOT YET IMPLEMENTED.

Planned: checker.py runs during company research (workflow step 2) - passes the
extracted company text + all Preference rows to the LLM provider, asks whether the
company conflicts with any of them. Output must be evidence-first: each flagged
preference returns the on-site snippet + source URL, plus severity and confidence.
A hard-exclusion match blocks drafting until the owner confirms; soft matches are
surfaced as notes. This is a flag for the owner's judgment, never an automatic verdict.
"""
