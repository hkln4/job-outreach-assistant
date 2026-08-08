"""
Recommendation, drafting, and review (CLAUDE.md section 5, steps 3-6). NOT YET IMPLEMENTED.

Planned:
- drafting.py   recommend target position + CV variant (with written reasoning) from
                Profile + Company.research, then draft a sourced cover-letter email.
                Evidence Rule: every company-specific claim needs a source URL.
- review.py     approve -> send via app.email_send.gmail_client with the chosen CV
                attached; reject -> owner free-text feedback -> regenerate, looped
                until approved; each cycle appended to Application.revision_history.
"""
