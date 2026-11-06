"""The owner's identity block - signature under every letter, contact row on
generated CVs. Written by the app, never by a model (roadmap phase 4).

Auto-detected from the owner's own CV first (name on the first line, title on
the second, then email / phone / LinkedIn / GitHub / city found by regex);
anything the owner typed on the Preferences page wins. The portfolio URL is
the owner's to give - it is not guessed.
"""
import re

from sqlalchemy.orm import Session

from app.models import CV, OwnerSettings
from app.profile_builder.cv_parser import try_extract_text

IDENTITY_FIELDS = ("full_name", "title", "email", "phone", "city", "linkedin_url", "github_url", "portfolio_url")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+?90[\s-]?)?\(?0?5\d{2}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}|\+\d{10,13}")
LINKEDIN_RE = re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+/?", re.IGNORECASE)
GITHUB_RE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/[A-Za-z0-9\-]+/?(?![A-Za-z0-9\-/])", re.IGNORECASE)
CITY_RE = re.compile(r"\b(Ankara|İstanbul|Istanbul|İzmir|Izmir|Bursa|Antalya|Eskişehir|Eskisehir|Kocaeli|Konya|Kayseri)\b(?:,\s*(Turkey|Türkiye))?")
SIGNATURE_LABELS = {"tr": {"phone": "Tel"}, "en": {"phone": "Phone"}}


def _url(value: str) -> str:
    value = value.strip().rstrip("/")
    return value if not value or value.startswith("http") else f"https://{value}"


def detect_identity(cv_text: str) -> dict:
    """Identity fields as the CV states them (FlowCV-style header: name,
    title, then one contact row)."""
    lines = [line.strip() for line in (cv_text or "").splitlines() if line.strip()]
    head = "\n".join(lines[:8])
    found = {}
    if lines and not EMAIL_RE.search(lines[0]) and len(lines[0].split()) <= 5:
        found["full_name"] = lines[0]
    if len(lines) > 1 and not EMAIL_RE.search(lines[1]) and len(lines[1].split()) <= 5 and not any(ch.isdigit() for ch in lines[1]):
        found["title"] = lines[1]
    for field, regex in (("email", EMAIL_RE), ("phone", PHONE_RE), ("linkedin_url", LINKEDIN_RE), ("github_url", GITHUB_RE)):
        match = regex.search(head)
        if match:
            value = match.group(0).strip()
            found[field] = _url(value) if field.endswith("_url") else value
    city = CITY_RE.search(head)
    if city:
        found["city"] = city.group(0)
    return found


def detected_from_cvs(db: Session) -> dict:
    """Detection over the owner's CV files, first file that has each field."""
    merged: dict = {}
    for cv in db.query(CV).filter(CV.file_path != "").order_by(CV.id).all():
        text, error = try_extract_text(cv.file_path)
        if error or not text:
            continue
        for key, value in detect_identity(text).items():
            merged.setdefault(key, value)
    return merged


def owner_identity(db: Session, detected: dict | None = None) -> dict:
    """Typed values win; empty ones fall back to the CV detection."""
    settings = db.query(OwnerSettings).first()
    detected = detected if detected is not None else detected_from_cvs(db)
    return {field: (getattr(settings, field, "") if settings else "") or detected.get(field, "") for field in IDENTITY_FIELDS}


def signature_block(identity: dict, language: str = "tr", portfolio_link: str = "") -> str:
    """The closing block the app appends under the letter body."""
    lines = [identity.get("full_name", "")]
    if identity.get("title"):
        lines.append(identity["title"])
    labels = SIGNATURE_LABELS.get(language, SIGNATURE_LABELS["en"])
    if identity.get("phone"):
        lines.append(f"{labels['phone']}: {identity['phone']}")
    for field in ("email", "linkedin_url", "github_url"):
        if identity.get(field):
            lines.append(identity[field])
    portfolio = portfolio_link or identity.get("portfolio_url", "")
    if portfolio:
        lines.append(portfolio)
    return "\n".join(line for line in lines if line)


def strip_trailing_name(body: str, name: str) -> str:
    """Drop a closing name line the model wrote anyway (the prompt says not to)."""
    body = (body or "").rstrip()
    lines = body.splitlines()
    if name and lines and lines[-1].strip().casefold() == name.strip().casefold():
        body = "\n".join(lines[:-1]).rstrip()
    return body


def with_signature(body: str, signature: str) -> str:
    """Body + signature (a model-written name line is replaced, not doubled)."""
    name = signature.splitlines()[0] if signature else ""
    body = strip_trailing_name(body, name)
    return f"{body}\n\n{signature}" if signature else body
