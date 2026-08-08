"""
One-off manual check: sends yourself a test email to verify OAuth + send work
end-to-end. First run opens a browser consent screen (log in with the same
Gmail account you added as a Test user). Run: python -m scripts.gmail_smoke_test
"""
from app.config import get_settings
from app.email_send.gmail_client import send_email, thread_has_reply

if __name__ == "__main__":
    settings = get_settings()
    if not settings.gmail_sender_address:
        raise SystemExit("Set GMAIL_SENDER_ADDRESS in .env first.")

    result = send_email(
        to=settings.gmail_sender_address,
        subject="Gmail API smoke test",
        body_text="If you're reading this in your inbox, OAuth + send are working.",
    )
    print("Sent:", result)
    print("Reply check right after sending (expect False):", thread_has_reply(result["threadId"]))
