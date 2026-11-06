"""One banned-phrase list (roadmap phase 4), shared by the drafting prompt and
the deterministic "check before sending" (draft_checks.py) - so what the
prompt forbids and what the check flags can never drift apart."""

BANNED_PHRASES: tuple[str, ...] = (
    # Turkish stock cover-letter phrases
    "katkı sağlayabileceğime inanıyorum",
    "katkı sağlayabileceğime",
    "ilgiyle takip ediyorum",
    "takip ediyorum",
    "bilgi ve deneyimlerimle örtüşüyor",
    "örtüştüğünü düşünüyorum",
    "sağlam bir temel",
    "heyecan duyuyorum",
    "heyecanla",
    "değerli ekibinize",
    "değerli ekibinizin",
    "fırsat tanınması",
    "fırsat tanımanızı",
    "dinamik bir ekip",
    "kendimi geliştirebileceğim",
    "kendimi geliştirebileceğim bir ekipte",
    "vizyonunuzu paylaşıyorum",
    "tutkuluyum",
    # English
    "i believe i can contribute",
    "i have been following with great interest",
    "following with great interest",
    "i am excited to apply",
    "dynamic team",
    "passionate about",
    "valuable team",
    "i am confident that",
)


def prompt_list() -> str:
    """The list as the drafting prompt shows it."""
    return "\n".join(f'  "{phrase}",' for phrase in BANNED_PHRASES)


def found_in(text: str) -> list[str]:
    lowered = (text or "").casefold()
    found = [phrase for phrase in BANNED_PHRASES if phrase in lowered]
    # report "katkı sağlayabileceğime inanıyorum" once, not also its prefix
    return [p for p in found if not any(p != other and p in other for other in found)]
