"""One-off manual check: send a test email to any address via the app's real
Gmail integration - useful for testing the reply-check flow (reply to it from
that address, then use thread_has_reply() / the app's "Check for a reply"
button). Run: python -m scripts.send_test_email <to-address> ["subject"] ["body"]
"""
import sys

from app.email_send.gmail_client import send_email

if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python -m scripts.send_test_email <to-address> [subject] [body]")

    to = sys.argv[1]
    subject = sys.argv[2] if len(sys.argv) > 2 else "Test email"
    body = sys.argv[3] if len(sys.argv) > 3 else "This is a manual test email from the app's Gmail integration."

    result = send_email(to=to, subject=subject, body_text=body)
    print("Sent:", result)
    print(f"Thread ID: {result['threadId']}")
