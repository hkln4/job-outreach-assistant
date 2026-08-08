"""
Gmail send (CLAUDE.md sections 5 step 5, 9, 12). NOT YET IMPLEMENTED.

Planned: gmail_client.py - OAuth2 via google-api-python-client, sends the approved
Application with the exact chosen CV attached. Deliverability rules to enforce here
(section 9): validate recipient address before sending, delay between sends, never
send to the same company/contact twice (dedup check against existing Application/
Contact rows). No auto-send - this is only ever called after explicit UI approval.
"""
