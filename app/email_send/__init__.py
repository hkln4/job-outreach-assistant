"""
Gmail send (CLAUDE.md sections 5 step 5, 9, 12).

gmail_client.py: OAuth2 via google-api-python-client (send_email, thread_has_reply).
Still open: wiring send_email into the Application approval flow with the exact
chosen CV attached, the send-delay between sends, and the dedup check against
existing Application/Contact rows (section 9) - that lives in app.applications
once drafting/review is built. No auto-send - only ever called after explicit
UI approval.
"""
