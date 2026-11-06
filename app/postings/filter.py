"""The owner's posting filter, applied by code to everything the app finds.

Three rules, all owner-editable on the postings page:
- roles: the title must name at least one of them (with aliases: "ml" also
  matches "machine learning", "swe" matches "software engineer" ...), and
  none of the excluded words;
- level: intern / junior / mid (no level stated) / senior, from the title,
  else from the years of experience asked;
- location: the home city in any work mode; anywhere else (another city or
  abroad) only when the posting is remote.

Postings from boards and careers pages that don't match are counted per
reason and never imported. A posting the owner pastes is never dropped -
it just shows which rule it misses.
"""
import re

from app.applications.synonyms import fold
from app.postings.gates import JUNIOR_RE, SENIOR_RE, YEARS_RE, location_gate

LEVELS = ("intern", "junior", "mid", "senior")

DEFAULT_FILTER = {
    "roles": [
        "software engineer", "backend", "data scientist", "data engineer", "data analyst",
        "machine learning", "ai", "computer vision", "deep learning", "nlp", "mlops", "python",
    ],
    "exclude": ["sales", "frontend", "ios", "android", "sap", "manager"],
    "levels": ["intern", "junior", "mid"],
    "home_city": "Ankara",
    "elsewhere_remote_only": True,
}

# A word in the filter also matches these spellings in a title (folded,
# lowercase, Turkish included).
ALIASES = {
    "ml": ["machine learning", "makine ogrenmesi", "makine ogrenimi"],
    "machine learning": ["ml", "makine ogrenmesi", "makine ogrenimi"],
    "ai": ["artificial intelligence", "yapay zeka", "genai", "llm"],
    "artificial intelligence": ["ai", "yapay zeka"],
    "swe": ["software engineer", "software developer", "yazilim muhendisi", "yazilim gelistirici"],
    "software engineer": ["swe", "software developer", "yazilim muhendisi", "yazilim gelistirici", "software development engineer"],
    "software developer": ["software engineer", "yazilim gelistirici", "yazilim muhendisi"],
    "data scientist": ["data science", "veri bilimci", "veri bilimi"],
    "data engineer": ["data engineering", "veri muhendisi"],
    "data analyst": ["veri analisti", "data analytics"],
    "computer vision": ["goruntu isleme", "bilgisayarli goru", "image processing", "cv"],
    "backend": ["back-end", "back end", "backend developer", "arka uc"],
    "deep learning": ["derin ogrenme"],
    "nlp": ["natural language processing", "dogal dil isleme"],
    "mlops": ["ml ops", "machine learning operations"],
}
INTERN_RE = re.compile(r"\b(intern(ship)?|stajyer|staj|trainee)\b", re.IGNORECASE)
MANAGER_RE = re.compile(r"\b(head of|director|direktor|vp|chief|manager|yonetici|mudur|architect|mimar)\b", re.IGNORECASE)
REMOTE_RE = re.compile(r"\b(remote|uzaktan|work from home|wfh|anywhere|fully[- ]remote|tamamen uzaktan)\b", re.IGNORECASE)
HYBRID_RE = re.compile(r"\b(hybrid|hibrit)\b", re.IGNORECASE)


def normalized(raw: dict | None) -> dict:
    """The stored filter with defaults filled in."""
    data = {**DEFAULT_FILTER, **(raw or {})}
    data["roles"] = [r.strip() for r in data.get("roles") or [] if r.strip()]
    data["exclude"] = [r.strip() for r in data.get("exclude") or [] if r.strip()]
    data["levels"] = [lv for lv in data.get("levels") or [] if lv in LEVELS]
    return data


def parse_terms(text: str) -> list[str]:
    """"junior swe, data scientist; ml/ai" -> ["junior swe", "data scientist", "ml", "ai"]."""
    return [t.strip() for t in re.split(r"[,;\n/]+", text or "") if t.strip()]


def _has(term: str, text: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(fold(term))}(?![\w])", text) is not None


def _matches_term(term: str, title: str) -> bool:
    # every word of a multi-word term must be there ("junior swe" = junior AND swe)
    words = fold(term).split()
    if len(words) > 1 and not _has(term, title):
        if all(any(_has(alt, title) for alt in [w] + ALIASES.get(w, [])) for w in words):
            return True
    return any(_has(alt, title) for alt in [term] + ALIASES.get(fold(term), []))


def level_of(title: str, description: str = "") -> str:
    t = title or ""
    if INTERN_RE.search(fold(t)):
        return "intern"
    if JUNIOR_RE.search(t):
        return "junior"
    if SENIOR_RE.search(t) or MANAGER_RE.search(fold(t)):
        return "senior"
    years = [int(m.group(1)) for m in YEARS_RE.finditer(description or "")]
    if years:
        most = max(years)
        return "junior" if most <= 1 else ("mid" if most <= 4 else "senior")
    return "mid"


def work_mode_of(location: str, title: str, work_mode: str = "", description: str = "") -> str:
    mode = (work_mode or "").lower()
    if mode in ("remote", "hybrid", "onsite", "on-site"):
        return "onsite" if mode == "on-site" else mode
    text = fold(f"{title} {location}")
    if REMOTE_RE.search(text):
        return "remote"
    if HYBRID_RE.search(text):
        return "hybrid"
    head = fold((description or "")[:600])
    if REMOTE_RE.search(head):
        return "remote"
    return "unknown"


def check(rules: dict, *, title: str, location: str = "", description: str = "", work_mode: str = "", unknown_location_ok: bool = False) -> dict:
    """{"match": bool, "fails": [{"rule": role|level|location, "why": ...}],
    "level": ..., "work_mode": ...}. Every failing rule is listed, so the
    owner sees why something was left out."""
    rules = normalized(rules)
    folded_title = fold(title or "")
    fails = []

    excluded = [w for w in rules["exclude"] if _matches_term(w, folded_title)]
    if excluded:
        fails.append({"rule": "role", "why": f"excluded word: {excluded[0]}"})
    elif rules["roles"] and not any(_matches_term(r, folded_title) for r in rules["roles"]):
        fails.append({"rule": "role", "why": "none of your roles in the title"})

    level = level_of(title, description)
    if rules["levels"] and level not in rules["levels"]:
        fails.append({"rule": "level", "why": level})

    mode = work_mode_of(location, title, work_mode, description)
    home = fold(rules.get("home_city") or "")
    place = fold(location or "")
    gate = location_gate(location, description)
    if gate["result"] == "reject" and "not open" in gate.get("note", ""):
        fails.append({"rule": "location", "why": gate["note"]})
    elif home and _has(home, place):
        pass  # the home city: any work mode
    elif mode == "remote":
        pass  # elsewhere, remote - fine
    elif not place.strip():
        if not unknown_location_ok:
            fails.append({"rule": "location", "why": "no location given"})
    elif rules.get("elsewhere_remote_only", True):
        fails.append({"rule": "location", "why": f"{location} and not remote"})
    return {"match": not fails, "fails": fails, "level": level, "work_mode": mode}


def owner_filter(db) -> dict:
    from app.models import OwnerSettings

    settings = db.query(OwnerSettings).first()
    return normalized(settings.posting_filter if settings else None)


def check_posting(rules: dict, posting) -> dict:
    """The filter on a stored JobPosting (careers-page postings without a
    location fall back to their company's location)."""
    location = posting.location or ""
    if not location and posting.company is not None:
        location = posting.company.location or ""
    return check(
        rules,
        title=posting.title or "",
        location=location,
        description=posting.description_text or "",
        work_mode=posting.work_mode or "",
        unknown_location_ok=posting.source in ("careers_page", "manual", "linkedin"),
    )
