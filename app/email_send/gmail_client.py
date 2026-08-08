"""
Gmail send + reply-check (CLAUDE.md section 5 step 5, section 9 deliverability rules).

OAuth2: first call opens a browser for consent, then caches the token at
GMAIL_TOKEN_PATH so later calls are silent. Scopes cover both sending and
reading - Gmail's OAuth model has no thread-scoped grant, so instead this
module scopes itself: thread_has_reply() only ever looks up a thread_id the
app already created and stored (Application.gmail_thread_id), never scans
the inbox at large.
"""
import base64
import mimetypes
import os
import re
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from app.config import get_settings

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_email(address: str) -> bool:
    """Deliverability rule (section 9): validate the recipient before sending."""
    return bool(_EMAIL_RE.match(address.strip()))


def get_credentials() -> Credentials:
    settings = get_settings()
    token_path = settings.gmail_token_path
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(settings.gmail_credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return creds


def get_gmail_service():
    return build("gmail", "v1", credentials=get_credentials())


def _build_message(to: str, subject: str, body_text: str, attachment_path: str | None = None) -> dict:
    # No explicit "from" header: Gmail stamps it from the authenticated account
    # (userId="me") itself. Setting it manually here caused SPF/DKIM alignment
    # to fail and Gmail to flag the message with a "sender not verified" warning.
    message = MIMEMultipart()
    message["to"] = to
    message["subject"] = subject
    message.attach(MIMEText(body_text))

    if attachment_path:
        ctype, _ = mimetypes.guess_type(attachment_path)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with open(attachment_path, "rb") as f:
            part = MIMEApplication(f.read(), _subtype=subtype)
        part.add_header("Content-Disposition", "attachment", filename=os.path.basename(attachment_path))
        message.attach(part)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return {"raw": raw}


def send_email(to: str, subject: str, body_text: str, attachment_path: str | None = None) -> dict:
    """Sends via the owner's Gmail account. Returns {"id", "threadId"}.
    Caller is responsible for the section-9 dedup check before calling this -
    this function does not check whether `to` was already emailed."""
    if not validate_email(to):
        raise ValueError(f"Invalid recipient address: {to!r}")

    service = get_gmail_service()
    message = _build_message(to, subject, body_text, attachment_path)
    sent = service.users().messages().send(userId="me", body=message).execute()
    return {"id": sent["id"], "threadId": sent["threadId"]}


def thread_has_reply(thread_id: str) -> bool:
    """Headers-only check (no message body fetched): does this thread contain
    a message not sent by the owner?"""
    settings = get_settings()
    service = get_gmail_service()
    thread = (
        service.users()
        .threads()
        .get(userId="me", id=thread_id, format="metadata", metadataHeaders=["From"])
        .execute()
    )
    for msg in thread.get("messages", []):
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        sender = headers.get("From", "")
        if settings.gmail_sender_address and settings.gmail_sender_address not in sender:
            return True
    return False
