"""Preference keyword pre-screen (CLAUDE.md section 8: deterministic first).
`Preference.signals` ("savunma", "defence", "askeri") are matched in page text
by code before any LLM call. A hit is a flag with its snippet for the owner -
never an automatic rejection, and never a replacement for the LLM check."""
import re

from app.applications.synonyms import fold
from app.models import Preference

SNIPPET_CHARS = 80


def keyword_flags(text: str, preferences: list[Preference], url: str = "") -> list[dict]:
    """[{preference_id, type, description, keyword, evidence_snippet, url}] -
    one per preference, the first signal that appears as a whole word (Turkish
    suffixes allowed: "savunma" matches "savunmaya")."""
    folded = fold(text or "")
    flags = []
    for preference in preferences:
        for signal in preference.signals or []:
            word = fold(signal).strip()
            if len(word) < 3:
                continue
            match = re.search(rf"(?<!\w){re.escape(word)}\w*", folded)
            if match:
                start, end = max(0, match.start() - SNIPPET_CHARS), match.end() + SNIPPET_CHARS
                # fold() keeps length for Turkish text, so the original span lines up
                snippet = re.sub(r"\s+", " ", (text or "")[start:end]).strip()
                flags.append(
                    {
                        "preference_id": preference.id,
                        "type": preference.type.value,
                        "description": preference.description,
                        "keyword": signal,
                        "evidence_snippet": snippet,
                        "url": url,
                    }
                )
                break
    return flags
