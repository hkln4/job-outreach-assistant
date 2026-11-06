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


def run_gates(title: str, location: str, description: str, target_roles: list[str] | None = None, skills: list[str] | None = None) -> dict:
    return {
        "role": role_gate(title, description, target_roles, skills),
        "location": location_gate(location, description),
        "seniority": seniority_gate(title, description),
        "language": language_gate(description),
    }


def owner_targets(db) -> tuple[list[str], list[str]]:
    """The owner's target roles and core skill names, for the role gate."""
    from app.models import Profile, TechSkill

    profile = db.query(Profile).first()
    skills = [s.name for s in db.query(TechSkill).filter(TechSkill.tier == "core", TechSkill.hidden.is_(False)).all()]
    return (profile.target_roles if profile else []), skills


def gates_block(gates: dict) -> bool:
    """A rejected location or a clearly unrelated role blocks the fit call;
    gaps are shown, never auto-dismissed."""
    return (gates.get("location") or {}).get("result") == "reject" or (gates.get("role") or {}).get("result") == "reject"


# --- Role relevance (code): keeps sales, finance, HR ... postings out ---
TECH_TITLE_RE = re.compile(
    r"\b(engineer\w*|developer|scientist|data|machine learning|ml|ai|artificial intelligence|computer vision|"
    r"deep learning|nlp|backend|back-end|software|devops|mlops|research\w*|analyst|analytics|python|"
    r"mühendis\w*|muhendis\w*|geliştirici|gelistirici|yazılım|yazilim|veri|yapay zeka|görüntü|goruntu|araştırma\w*|arastirma\w*)\b",
    re.IGNORECASE,
)
NON_TECH_TITLE_RE = re.compile(
    r"\b(sales|satış|satis|account (manager|executive)|business development|accountant|accounting|muhasebe|finance|finans|"
    r"hr|human resources|insan kaynakları|insan kaynaklari|recruiter|talent acquisition|marketing|pazarlama|"
    r"customer (support|success|service)|müşteri (hizmet|temsilci)|call center|çağrı merkezi|legal|hukuk|lawyer|avukat|"
    r"office (manager|assistant)|receptionist|resepsiyon|driver|şoför|sofor|warehouse|depo|logistics coordinator|"
    r"graphic designer|grafik tasarım|content writer|copywriter|social media|sosyal medya|nurse|hemşire|cashier|kasiyer|"
    r"technician|teknisyen|operator|operatör)\b",
    re.IGNORECASE,
)


def role_gate(title: str, description: str, target_roles: list[str] | None = None, skills: list[str] | None = None) -> dict:
    """pass / reject / ambiguous for whether a posting is the owner's kind of
    job at all, with the deciding words. Reject only a clearly non-technical
    title with nothing in the posting touching the owner's targets."""
    from app.applications.synonyms import concepts_in, tokens

    title = title or ""
    target_text = " ".join(target_roles or [])
    overlap = (concepts_in(f"{title} {description[:3000]}") & concepts_in(target_text + " machine learning computer vision backend data engineering"))
    role_words = tokens(title) & tokens(target_text) - {"junior", "senior", "intern", "engineer", "specialist"}
    skill_hits = [s for s in skills or [] if len(s) > 2 and re.search(rf"(?<!\w){re.escape(fold(s))}(?!\w)", fold(title))]
    tech = TECH_TITLE_RE.search(title)
    non_tech = NON_TECH_TITLE_RE.search(title)
    if non_tech and not (tech or role_words or skill_hits):
        return {"result": "reject", "quote": title, "note": f"not a technical role ({non_tech.group(0)})"}
    if tech or role_words or skill_hits or overlap:
        reason = (tech.group(0) if tech else "") or ", ".join(sorted(role_words) or skill_hits or sorted(overlap))
        return {"result": "pass", "quote": title, "note": f"matches your targets ({reason})"}
    return {"result": "ambiguous", "quote": title, "note": "no clear link to your target roles"}


def is_relevant(gates: dict) -> bool:
    return (gates.get("role") or {}).get("result") != "reject"
