"""
Gmail send + reply-check (CLAUDE.md section 5 step 5, section 9 deliverability rules).

OAuth2: first call opens a browser for consent, then caches the token at
GMAIL_TOKEN_PATH so later calls are silent. Scopes cover both sending and
reading - Gmail's OAuth model has no thread-scoped grant, so instead this
module scopes itself: classify_thread() only looks up a thread_id the app
created and stored (Application.gmail_thread_id), and the searches below are
narrow by construction (one company's domain since one send; our own Sent
folder) - never a scan of the inbox at large.
"""
import base64
import mimetypes
import os
import re
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid, parseaddr

from google.auth.exceptions import RefreshError
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
        refreshed = False
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                refreshed = True
            except RefreshError:
                # invalid_grant: the refresh token was revoked or expired (an OAuth
                # app in "Testing" mode gets 7-day refresh tokens) - ask for consent again.
                pass
        if not refreshed:
            flow = InstalledAppFlow.from_client_secrets_file(settings.gmail_credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return creds


def get_gmail_service():
    return build("gmail", "v1", credentials=get_credentials())


def _build_message(
    to: str,
    subject: str,
    body_text: str,
    attachment_path: str | None = None,
    attachment_name: str | None = None,
    message_id: str | None = None,
) -> dict:
    # No explicit "from" header: Gmail stamps it from the authenticated account
    # (userId="me") itself. Setting it manually here caused SPF/DKIM alignment
    # to fail and Gmail to flag the message with a "sender not verified" warning.
    message = MIMEMultipart()
    message["to"] = to
    message["subject"] = subject
    if message_id:
        message["Message-ID"] = message_id
    message.attach(MIMEText(body_text))

    if attachment_path:
        ctype, _ = mimetypes.guess_type(attachment_path)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with open(attachment_path, "rb") as f:
            part = MIMEApplication(f.read(), _subtype=subtype)
        # The recipient sees this name - the disk name carries a uniqueness
        # suffix, so callers pass a clean one.
        filename = attachment_name or os.path.basename(attachment_path)
        part.add_header("Content-Disposition", "attachment", filename=filename)
        message.attach(part)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return {"raw": raw}


def send_email(
    to: str,
    subject: str,
    body_text: str,
    attachment_path: str | None = None,
    attachment_name: str | None = None,
    message_id: str | None = None,
) -> dict:
    """Sends via the owner's Gmail account. Returns {"id", "threadId"}.
    `message_id` (new_message_id()) is set as the RFC Message-ID header, so a
    send that raised after Gmail accepted it can still be found.
    Caller is responsible for the section-9 dedup check before calling this -
    this function does not check whether `to` was already emailed."""
    if not validate_email(to):
        raise ValueError(f"Invalid recipient address: {to!r}")

    service = get_gmail_service()
    message = _build_message(to, subject, body_text, attachment_path, attachment_name, message_id)
    sent = service.users().messages().send(userId="me", body=message).execute()
    return {"id": sent["id"], "threadId": sent["threadId"]}


def send_reply(
    thread_id: str,
    prior_gmail_message_id: str,
    to: str,
    subject: str,
    body_text: str,
) -> dict:
    """Sends a follow-up INSIDE an existing thread. Gmail only groups a sent
    message into a thread when the API threadId is set AND the RFC 2822
    In-Reply-To/References headers point at the prior message AND the subject
    matches - so the original message's Message-ID header is fetched first.
    Returns {"id", "threadId"}."""
    if not validate_email(to):
        raise ValueError(f"Invalid recipient address: {to!r}")

    service = get_gmail_service()
    prior = (
        service.users()
        .messages()
        .get(userId="me", id=prior_gmail_message_id, format="metadata", metadataHeaders=["Message-ID"])
        .execute()
    )
    prior_headers = {h["name"].lower(): h["value"] for h in prior.get("payload", {}).get("headers", [])}
    rfc_message_id = prior_headers.get("message-id", "")

    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    message = MIMEMultipart()
    message["to"] = to
    message["subject"] = subject
    if rfc_message_id:
        message["In-Reply-To"] = rfc_message_id
        message["References"] = rfc_message_id
    message.attach(MIMEText(body_text))

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    sent = (
        service.users().messages().send(userId="me", body={"raw": raw, "threadId": thread_id}).execute()
    )
    return {"id": sent["id"], "threadId": sent["threadId"]}


# --- Reply classification (bounce / auto-reply / real reply) ---

THREAD_HEADERS = [
    "From", "Subject", "Date", "Auto-Submitted", "X-Autoreply", "X-Autorespond",
    "Precedence", "X-Failed-Recipients", "Content-Type",
]
BOUNCE_SENDER_LOCALS = ("mailer-daemon", "postmaster")
AUTO_REPLY_SUBJECT_PREFIXES = (
    "automatic reply", "auto:", "autoreply", "out of office", "otomatik yanıt",
    "otomatik yanit", "otomatik cevap", "ofis dışında", "ofis disinda",
)
AUTO_PRECEDENCE = ("auto_reply", "bulk", "junk")


def classify_message(headers: dict[str, str], owner_address: str = "", labels: list[str] | None = None) -> tuple[str, str]:
    """("own" | "bounce" | "auto_reply" | "reply", detail) for one message's
    headers. Senders are compared by parsed address, case-insensitively; a
    message Gmail labels SENT is always our own, even with no owner address
    configured."""
    h = {key.lower(): value or "" for key, value in headers.items()}
    _, sender = parseaddr(h.get("from", ""))
    sender = sender.lower()
    if "SENT" in (labels or []) or (owner_address and sender == owner_address.strip().lower()):
        return "own", ""

    subject = h.get("subject", "").strip()
    local = sender.split("@", 1)[0]
    content_type = h.get("content-type", "").lower()
    if (
        local in BOUNCE_SENDER_LOCALS
        or h.get("x-failed-recipients")
        or ("multipart/report" in content_type and "delivery-status" in content_type)
    ):
        failed = h.get("x-failed-recipients", "").strip()
        return "bounce", failed or subject

    auto_submitted = h.get("auto-submitted", "").strip().lower()
    if (
        (auto_submitted and auto_submitted != "no")
        or h.get("x-autoreply")
        or h.get("x-autorespond")
        or h.get("precedence", "").strip().lower() in AUTO_PRECEDENCE
        or subject.casefold().startswith(AUTO_REPLY_SUBJECT_PREFIXES)
    ):
        return "auto_reply", subject
    return "reply", f"{sender}: {subject}"


def classify_thread_messages(messages: list[dict], owner_address: str = "") -> dict:
    """{"kind": "reply" | "bounce" | "auto_reply" | "none", "detail", "message_id"}.
    A real reply anywhere in the thread wins over a bounce or an auto-reply."""
    found: dict[str, dict] = {}
    for message in messages:
        headers = {h["name"]: h["value"] for h in message.get("payload", {}).get("headers", [])}
        kind, detail = classify_message(headers, owner_address, message.get("labelIds"))
        if kind != "own" and kind not in found:
            found[kind] = {"kind": kind, "detail": detail, "message_id": message.get("id", "")}
    for kind in ("reply", "bounce", "auto_reply"):
        if kind in found:
            return found[kind]
    return {"kind": "none", "detail": "", "message_id": ""}


def classify_thread(thread_id: str) -> dict:
    """Headers-only look at the application's own thread (see classify_thread_messages)."""
    service = get_gmail_service()
    thread = (
        service.users()
        .threads()
        .get(userId="me", id=thread_id, format="metadata", metadataHeaders=THREAD_HEADERS)
        .execute()
    )
    return classify_thread_messages(thread.get("messages", []), get_settings().gmail_sender_address)


def search_messages(query: str, limit: int = 10) -> list[dict]:
    """[{id, threadId, from, subject, date}] for a Gmail search - headers only."""
    service = get_gmail_service()
    listing = service.users().messages().list(userId="me", q=query, maxResults=limit).execute()
    results = []
    for ref in listing.get("messages", []) or []:
        message = (
            service.users()
            .messages()
            .get(userId="me", id=ref["id"], format="metadata", metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
        headers = {h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])}
        results.append(
            {
                "id": message.get("id", ref["id"]),
                "threadId": message.get("threadId", ref.get("threadId", "")),
                "from": headers.get("from", ""),
                "subject": headers.get("subject", ""),
                "date": headers.get("date", ""),
                "labels": message.get("labelIds", []),
            }
        )
    return results


def possible_replies(domain: str, after: datetime, exclude_thread_id: str | None = None) -> list[dict]:
    """Messages from anyone at the company's domain since the send, outside
    the application's thread - shown as "possible reply - confirm", never
    applied automatically."""
    if not domain:
        return []
    query = f"from:(@{domain}) after:{after.strftime('%Y/%m/%d')}"
    return [m for m in search_messages(query) if m["threadId"] != exclude_thread_id and "SENT" not in m["labels"]]


def find_sent_message(rfc_message_id: str, to: str, subject: str, after: datetime) -> dict | None:
    """{id, threadId} of a message we sent, if Gmail has it. Used after a send
    raised (timeout): by our own Message-ID first, then - in case Gmail
    replaced that header - by recipient + subject in the Sent folder."""
    queries = []
    if rfc_message_id:
        queries.append(f"in:sent rfc822msgid:{rfc_message_id.strip('<>')}")
    clean_subject = subject.replace('"', "")
    queries.append(f'in:sent to:{to} subject:"{clean_subject}" after:{int(after.timestamp())}')
    for query in queries:
        for message in search_messages(query, limit=5):
            return {"id": message["id"], "threadId": message["threadId"]}
    return None


def new_message_id() -> str:
    """An RFC 2822 Message-ID in the sender's domain."""
    sender = get_settings().gmail_sender_address
    domain = sender.split("@", 1)[1] if "@" in sender else "mail.gmail.com"
    return make_msgid(domain=domain)
