"""Deterministic posting gates (CLAUDE.md section 8: regex where the text is
explicit, before any LLM fit call). Each gate returns
{result: pass | gap | ambiguous | reject | unknown, quote, note} - the quote is
the posting text that decided it, so the owner sees why.

- location: the Turkey filter for ATS boards and pasted postings;
- remote eligibility: "Remote - US", "EU only", "must be authorized to work in";
- seniority: "5+ yıl", "at least 3 years", "senior" against a new graduate;
- language: the posting's own language (by code) and foreign languages it
  requires besides Turkish/English.
"""
import re

from app.applications.synonyms import fold

TURKEY_ACCEPT = ("turkey", "turkiye", "istanbul", "ankara", "izmir", "bursa", "antalya", "kocaeli", "eskisehir", "remote - turkey", "remote turkey")
AMBIGUOUS_LOCATIONS = ("emea", "europe", "remote", "anywhere", "worldwide", "global")
REJECT_PATTERNS = (
    r"\b(us|usa|u\.s\.|united states|canada|uk|united kingdom|eu|europe|germany|india)\s*only\b",
    r"remote\s*[-–(,:]\s*(us|usa|united states|canada|uk|united kingdom|germany|india|latam|apac)\b",
    r"must be (legally )?(authorized|eligible) to work in (?!turkey|türkiye|turkiye)",
    r"\b(us|u\.s\.) (citizens?|work authorization) (only|required)\b",
    r"requires? (us|u\.s\.) (citizenship|security clearance)",
)
YEARS_RE = re.compile(
    r"(?:(?:en az|minimum|min\.?|at least)\s*)?(\d{1,2})\s*(?:\+|\s*-\s*\d{1,2})?\s*(?:yıl|yil|sene|years?|yrs?)",
    re.IGNORECASE,
)
SENIOR_RE = re.compile(r"\b(senior|sr\.|lead|principal|staff engineer|kıdemli|kidemli|takım lideri|team lead)\b", re.IGNORECASE)
JUNIOR_RE = re.compile(r"\b(junior|jr\.|entry[- ]level|new grad(uate)?|yeni mezun|stajyer|intern(ship)?|graduate program)\b", re.IGNORECASE)
FOREIGN_LANGUAGES = {
    "german": ("german", "almanca", "deutsch"), "french": ("french", "fransızca", "fransizca"),
    "japanese": ("japanese", "japonca"), "russian": ("russian", "rusça", "rusca"), "arabic": ("arabic", "arapça", "arapca"),
    "spanish": ("spanish", "ispanyolca", "İspanyolca"), "chinese": ("chinese", "mandarin", "çince", "cince"),
}
REQUIRED_WORDS = ("required", "must", "fluent", "zorunlu", "şart", "sart", "akıcı", "akici", "gerekli", "native")
TURKISH_STOPWORDS = {"ve", "bir", "için", "icin", "ile", "olan", "bu", "da", "de", "olarak", "en", "az", "deneyim", "sahip", "tercih", "yıl", "aranan", "nitelikler"}
ENGLISH_STOPWORDS = {"and", "the", "for", "with", "you", "our", "will", "of", "to", "in", "experience", "years", "we", "is", "are"}
OWNER_MAX_YEARS = 1  # a new graduate: up to ~1 year asked is fine


def _quote(text: str, start: int, end: int, pad: int = 60) -> str:
    return re.sub(r"\s+", " ", text[max(0, start - pad) : end + pad]).strip()


def detect_language(text: str) -> str:
    words = re.findall(r"\w+", (text or "").casefold())
    if not words:
        return ""
    # Words, not characters: one Turkish sentence in an English posting
    # ("En az 1 yıl deneyim...") must not flip the whole posting to Turkish.
    tr = sum(w in TURKISH_STOPWORDS or bool(re.search(r"[ğışçöü]", w)) for w in words)
    en = sum(w in ENGLISH_STOPWORDS for w in words)
    return "tr" if tr > en else "en"


def location_gate(location: str, description: str = "") -> dict:
    """The Turkey filter: accept / ambiguous / reject, with the deciding text."""
    loc = fold(location or "")
    for pattern in REJECT_PATTERNS:
        for haystack, raw in ((loc, location), (fold(description), description)):
            match = re.search(pattern, haystack)
            if match:
                return {"result": "reject", "quote": _quote(raw or "", match.start(), match.end()), "note": "not open to candidates in Turkey"}
    for word in TURKEY_ACCEPT:
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", loc):
            return {"result": "pass", "quote": location, "note": "Turkey"}
    if not loc.strip():
        return {"result": "unknown", "quote": "", "note": "no location given"}
    for word in AMBIGUOUS_LOCATIONS:
        if re.search(rf"(?<!\w){word}(?!\w)", loc):
            return {"result": "ambiguous", "quote": location, "note": "may or may not include Turkey - check"}
    return {"result": "reject", "quote": location, "note": "outside Turkey"}


def seniority_gate(title: str, description: str) -> dict:
    """A gap (never a silent pass) when the posting asks for more experience
    than a new graduate has."""
    text = f"{title}\n{description}"
    years = []
    for match in YEARS_RE.finditer(text):
        context = fold(text[max(0, match.start() - 80) : match.end() + 80])
        if any(w in context for w in ("deneyim", "tecrube", "experience", "calisma", "work")):
            years.append((int(match.group(1)), match))
    if years:
        # A junior title asking for 3+ years is still a gap - the years decide.
        needed, match = max(years, key=lambda pair: pair[0])
        if needed > OWNER_MAX_YEARS:
            return {"result": "gap", "quote": _quote(text, match.start(), match.end()), "note": f"asks for {needed}+ years", "years": needed}
        return {"result": "pass", "quote": _quote(text, match.start(), match.end()), "note": f"{needed} year(s)", "years": needed}
    junior = JUNIOR_RE.search(title or "")
    if junior:
        return {"result": "pass", "quote": title, "note": f"junior-level ({junior.group(0)})", "years": 0}
    senior = SENIOR_RE.search(title or "")
    if senior:
        return {"result": "gap", "quote": title, "note": f"senior role ({senior.group(0)})", "years": None}
    return {"result": "unknown", "quote": "", "note": "experience level not stated", "years": None}


def language_gate(description: str) -> dict:
    """Foreign languages the posting requires besides Turkish and English."""
    text = description or ""
    folded = fold(text)
    for language, words in FOREIGN_LANGUAGES.items():
        for word in words:
            for match in re.finditer(rf"(?<!\w){re.escape(fold(word))}", folded):
                context = folded[max(0, match.start() - 60) : match.end() + 60]
                if any(fold(r) in context for r in REQUIRED_WORDS):
                    return {"result": "gap", "quote": _quote(text, match.start(), match.end()), "note": f"requires {language}"}
    return {"result": "pass", "quote": "", "note": "no extra language required", "posting_language": detect_language(text)}


def run_gates(title: str, location: str, description: str) -> dict:
    return {
        "location": location_gate(location, description),
        "seniority": seniority_gate(title, description),
        "language": language_gate(description),
    }


def gates_block(gates: dict) -> bool:
    """A rejected location blocks; gaps are shown, never auto-dismissed."""
    return (gates.get("location") or {}).get("result") == "reject"
