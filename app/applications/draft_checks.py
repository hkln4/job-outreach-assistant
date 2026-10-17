"""Deterministic checks on a draft before the owner approves it. They never
block a send - the owner is the reviewer - they point at what to look at:
things the app had to correct (invented sources, a failed evidence pick) and
things the prompt asks for but the model may not have delivered."""
import re

from app.models import Application, ApplicationState

# The prompt asks for 130-180 words (Turkish) or 100-150 (English); flag only clear misses.
MIN_WORDS = 80
MAX_WORDS = 230

# Stock phrases DRAFT_SYSTEM_PROMPT bans - flagged in case the model slips anyway.
BANNED_PHRASES = (
    "takip ediyorum",
    "katkı sağlayabileceğime",
    "kendimi geliştirebileceğim bir ekipte",
    "örtüştüğünü düşünüyorum",
    "sağlam bir temel",
    "i believe i can contribute",
    "following with great interest",
)


def _words(text: str) -> int:
    return len(re.findall(r"\w+", text))


def draft_warnings(application: Application) -> list[tuple[str, str]]:
    """(i18n key, detail) pairs for the review page; empty when nothing to flag."""
    if application.state != ApplicationState.PENDING_APPROVAL:
        return []
    warnings: list[tuple[str, str]] = []
    notes = (application.revision_history or [{}])[0]
    body = application.cover_letter_body or ""

    if not (application.subject or "").strip() or not body.strip():
        warnings.append(("draft_check.empty", ""))
    else:
        count = _words(body)
        if count < MIN_WORDS or count > MAX_WORDS:
            warnings.append(("draft_check.word_count", str(count)))

    lowered = body.casefold()
    found = [phrase for phrase in BANNED_PHRASES if phrase in lowered]
    if found:
        warnings.append(("draft_check.cliche", ", ".join(f'"{p}"' for p in found)))

    if notes.get("dropped_sources"):
        warnings.append(("draft_check.dropped_sources", ", ".join(notes["dropped_sources"])))
    if not application.sources:
        warnings.append(("draft_check.no_sources", ""))

    if notes.get("evidence_fallback"):
        warnings.append(("draft_check.evidence_fallback", notes.get("evidence_error", "")))
    if application.evidence and not any(e.get("used") for e in application.evidence):
        warnings.append(("draft_check.no_evidence_used", ""))
    if notes.get("unsupported_claims"):
        warnings.append(("draft_check.unsupported_claims", "; ".join(notes["unsupported_claims"])))
    return warnings
