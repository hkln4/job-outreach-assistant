"""Deterministic checks on a draft before the owner approves it. They never
block a send - the owner is the reviewer - they point at what to look at:
things the app had to correct (invented sources, a failed evidence pick) and
things the prompt asks for but the model may not have delivered."""
import re

from app.applications.phrases import found_in
from app.models import Application, ApplicationState

# The prompt asks for 130-180 words (Turkish) or 100-150 (English); flag only clear misses.
MIN_WORDS = 80
MAX_WORDS = 230

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

    found = found_in(body)
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
    misses = notes.get("grounding_misses") or {}
    if misses.get("numbers"):
        warnings.append(("draft_check.ungrounded_numbers", ", ".join(misses["numbers"])))
    if misses.get("technologies"):
        warnings.append(("draft_check.ungrounded_technologies", ", ".join(misses["technologies"])))
    if notes.get("dropped_angles"):
        warnings.append(("draft_check.dropped_angles", str(notes["dropped_angles"])))
    claim_check = notes.get("claim_check") or {}
    for entry in claim_check.get("unsupported") or []:
        warnings.append((f"draft_check.claim.{entry['reason']}", entry["sentence"]))
    if claim_check.get("stale"):
        warnings.append(("draft_check.claim_stale", ""))
    if notes.get("metadata_error"):
        warnings.append(("draft_check.metadata_error", notes["metadata_error"]))
    return warnings
