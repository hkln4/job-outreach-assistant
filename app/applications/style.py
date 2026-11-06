"""Style memory and recipient-aware tone (roadmap phase 4, items 3 and 7).

- StyleExample: approved letters the owner marked "good example"; the two
  most similar (same language, same CV target role, newest first) are shown
  to the writer as voice references.
- StyleNote: standing rules promoted from reject feedback; every active one
  goes into the writing prompt.
- Owner edits made before approving are stored as a diff on the application
  (revision history), so the owner's corrections are kept and reviewable.
- Recipient kind, by code: HR address, technical contact (CTO, lead,
  engineer), founder of a small company - each gets its own tone guidance.
"""
import difflib
import re

from sqlalchemy.orm import Session

from app.companies.workflow import classify_email
from app.models import Application, Contact, StyleExample, StyleNote
from app.postings.gates import detect_language

MAX_EXAMPLES = 2
MAX_EXAMPLE_CHARS = 1500

RECIPIENT_TONE = {
    "hr": "The reader is HR / recruiting: lead with the role and why the CV fits it; keep technical detail light and clear.",
    "technical": "The reader is technical (CTO / engineering lead): shorter, more technical, with one concrete project detail (what was built and how).",
    "founder": "The reader is a founder of a small company: say plainly what the owner could take off their plate, based on his records.",
    "general": "",
}
TECHNICAL_ROLE_RE = re.compile(r"\b(cto|chief technology|engineering manager|tech lead|technical lead|lead engineer|head of (engineering|ai|data|ml)|mühendislik müdürü|teknik lider|yazılım müdürü|ar-?ge müdürü|engineer|mühendis)\b", re.IGNORECASE)
FOUNDER_ROLE_RE = re.compile(r"\b(founder|co-founder|kurucu|ceo|genel müdür|owner|sahibi)\b", re.IGNORECASE)


def recipient_kind(db: Session, application: Application | None, company, to_address: str = "") -> str:
    contact = None
    if application is not None and application.id:
        contact = db.query(Contact).filter(Contact.application_id == application.id).first()
    if contact is not None:
        if FOUNDER_ROLE_RE.search(contact.role or ""):
            return "founder"
        if TECHNICAL_ROLE_RE.search(contact.role or ""):
            return "technical"
        return "hr" if classify_email(contact.email) == "hr" else "general"
    address = to_address or (company.contact_email if company else "") or ""
    return "hr" if address and classify_email(address) == "hr" else "general"


def style_examples(db: Session, language: str, target_role: str) -> list[StyleExample]:
    examples = db.query(StyleExample).order_by(StyleExample.id.desc()).all()
    same_language = [e for e in examples if not language or e.language == language] or examples
    ranked = sorted(same_language, key=lambda e: (e.target_role != target_role,), reverse=False)
    return ranked[:MAX_EXAMPLES]


def style_prompt_section(db: Session, language: str, target_role: str) -> str:
    parts = []
    notes = db.query(StyleNote).filter(StyleNote.active.is_(True)).order_by(StyleNote.id).all()
    if notes:
        parts.append("STANDING STYLE RULES (from the owner):\n" + "\n".join(f"- {note.text}" for note in notes))
    examples = style_examples(db, language, target_role)
    if examples:
        parts.append(
            "STYLE EXAMPLES (letters the owner approved - match the voice, never reuse their content):\n"
            + "\n\n".join(f"Subject: {e.subject}\n{e.body[:MAX_EXAMPLE_CHARS]}" for e in examples)
        )
    return "\n\n".join(parts)


def save_example(db: Session, application: Application) -> StyleExample:
    existing = db.query(StyleExample).filter(StyleExample.application_id == application.id).first()
    if existing is not None:
        return existing
    example = StyleExample(
        application_id=application.id,
        language=detect_language(application.cover_letter_body),
        target_role=application.cv.target_role if application.cv else "",
        subject=application.subject,
        body=application.cover_letter_body,
    )
    db.add(example)
    db.commit()
    return example


def edit_diff(before: str, after: str) -> str:
    return "\n".join(difflib.unified_diff((before or "").splitlines(), (after or "").splitlines(), "draft", "edited", lineterm=""))
