"""Where a posting is, and whether the owner's location rules accept it.

The owner keeps an ordered list of places, each with the work modes he
accepts there (e.g. Ankara: on-site, hybrid, remote; Istanbul: hybrid,
remote), plus two fixed rules: the rest of Turkey, and abroad. Abroad is only
ever accepted when the posting is open to candidates in Turkey - its
location names Turkey, or a region/global term the owner counts as open
(EMEA, worldwide ...). "Remote - Canada" is remote, but for Canada: rejected.

Everything here is code: the location field and work-mode field of the
posting (ATS boards give both), and the first lines of the description.
"""
import re

from app.applications.synonyms import fold

MODES = ("onsite", "hybrid", "remote")

# All 81 provinces (folded) plus the country names: a place that names one is in Turkey.
TURKEY_PLACES = (
    "turkey", "turkiye", "adana", "adiyaman", "afyonkarahisar", "afyon", "agri", "amasya", "ankara", "antalya", "artvin",
    "aydin", "balikesir", "bilecik", "bingol", "bitlis", "bolu", "burdur", "bursa", "canakkale", "cankiri", "corum",
    "denizli", "diyarbakir", "edirne", "elazig", "erzincan", "erzurum", "eskisehir", "gaziantep", "giresun", "gumushane",
    "hakkari", "hatay", "isparta", "mersin", "istanbul", "izmir", "kars", "kastamonu", "kayseri", "kirklareli", "kirsehir",
    "kocaeli", "konya", "kutahya", "malatya", "manisa", "kahramanmaras", "mardin", "mugla", "mus", "nevsehir", "nigde",
    "ordu", "rize", "sakarya", "samsun", "siirt", "sinop", "sivas", "tekirdag", "tokat", "trabzon", "tunceli", "sanliurfa",
    "usak", "van", "yozgat", "zonguldak", "aksaray", "bayburt", "karaman", "kirikkale", "batman", "sirnak", "bartin",
    "ardahan", "igdir", "yalova", "karabuk", "kilis", "osmaniye", "duzce", "gebze",
)
DEFAULT_OPEN_TERMS = ["turkey", "türkiye", "emea", "worldwide", "global", "anywhere", "work from anywhere"]
DEFAULT_LOCATION_RULES = {
    "places": [{"place": "Ankara", "modes": list(MODES)}],
    "turkey_modes": ["remote"],
    "abroad_modes": ["remote"],
    "open_terms": DEFAULT_OPEN_TERMS,
    "bare_remote": "skip",  # "Remote" with no country named: skip | show
}
REMOTE_RE = re.compile(r"\b(remote|uzaktan|work from home|wfh|fully[- ]remote|tamamen uzaktan|distributed)\b")
HYBRID_RE = re.compile(r"\b(hybrid|hibrit)\b")
ONSITE_RE = re.compile(r"\b(on[- ]?site|in[- ]office|office[- ]based|ofiste|yerinde)\b")
# Words that are about the work mode, not a place (so "Remote" alone names no country)
MODE_WORDS_RE = re.compile(r"\b(remote|uzaktan|hybrid|hibrit|on[- ]?site|full[- ]time|part[- ]time|contract|tam zamanli|work from home|wfh|fully)\b")
RESTRICTED_RE = (
    re.compile(r"\b(must|should|need to|required to) (be )?(based|located|reside|live) in (?!turkey|turkiye|emea|europe)\w+"),
    re.compile(r"\bmust be (legally )?(authorized|eligible) to work in (?!turkey|turkiye)\w+"),
    re.compile(r"\b(us|u\.s\.|usa|uk|canada|eu) (citizens?|residents?|work authorization) (only|required)\b"),
    re.compile(r"\b(us|usa|uk|canada|eu|germany|india)[- ]only\b"),
)


def normalized_rules(raw: dict | None) -> dict:
    """The stored location rules, defaults filled in; older filters (one home
    city + "elsewhere remote only") are carried over."""
    raw = dict(raw or {})
    if "places" not in raw and raw.get("home_city"):
        raw["places"] = [{"place": raw["home_city"], "modes": list(MODES)}]
        raw.setdefault("turkey_modes", ["remote"] if raw.get("elsewhere_remote_only", True) else list(MODES))
    data = {**DEFAULT_LOCATION_RULES, **{k: v for k, v in raw.items() if k in DEFAULT_LOCATION_RULES}}
    data["places"] = [
        {"place": p.get("place", "").strip(), "modes": [m for m in p.get("modes") or [] if m in MODES]}
        for p in data.get("places") or [] if (p.get("place") or "").strip()
    ]
    for key in ("turkey_modes", "abroad_modes"):
        data[key] = [m for m in data.get(key) or [] if m in MODES]
    data["open_terms"] = [t.strip() for t in data.get("open_terms") or [] if t.strip()]
    if data.get("bare_remote") not in ("skip", "show"):
        data["bare_remote"] = "skip"
    return data


def _has(term: str, text: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(fold(term))}(?!\w)", text) is not None


def mode_of(location: str, title: str = "", work_mode: str = "", description: str = "") -> str:
    """onsite | hybrid | remote | unknown - the ATS field first, then the
    location and title, then the first lines of the description."""
    mode = (work_mode or "").lower().replace("-", "").replace("_", "")
    if mode in ("remote", "hybrid", "onsite", "inoffice", "office"):
        return "onsite" if mode in ("inoffice", "office") else mode
    for text in (fold(f"{location} {title}"), fold((description or "")[:600])):
        if HYBRID_RE.search(text):
            return "hybrid"
        if REMOTE_RE.search(text):
            return "remote"
        if ONSITE_RE.search(text):
            return "onsite"
    return "unknown"


def where(location: str, rules: dict) -> dict:
    """{"kind": place | turkey | open | abroad | bare_remote | none, "place": ...}"""
    text = fold(location or "")
    if not text.strip():
        return {"kind": "none", "place": ""}
    for rule in rules["places"]:
        if _has(rule["place"], text):
            return {"kind": "place", "place": rule["place"], "modes": rule["modes"]}
    if any(_has(p, text) for p in TURKEY_PLACES):
        return {"kind": "turkey", "place": location}
    if any(_has(t, text) for t in rules["open_terms"]):
        return {"kind": "open", "place": location}
    if not MODE_WORDS_RE.sub(" ", text).strip(" ,;-/()|"):
        return {"kind": "bare_remote", "place": location}  # "Remote", "Remote (full-time)"
    return {"kind": "abroad", "place": location}


def location_check(rules: dict, *, location: str, title: str = "", description: str = "", work_mode: str = "", unknown_ok: bool = False) -> dict:
    """{"ok": bool, "why": str, "mode": str, "kind": str}. "why" says exactly
    which rule decided, for the owner to see."""
    rules = normalized_rules(rules)
    mode = mode_of(location, title, work_mode, description)
    found = where(location, rules)
    effective = "onsite" if mode == "unknown" else mode  # no mode stated: usually on-site
    head = fold((description or "")[:1500])
    restricted = next((m.group(0) for r in RESTRICTED_RE for m in [r.search(head)] if m), "")

    def verdict(ok: bool, why: str) -> dict:
        return {"ok": ok, "why": why, "mode": mode, "kind": found["kind"]}

    if found["kind"] == "place":
        allowed = found["modes"]
        if effective in allowed:
            return verdict(True, f"{found['place']}: {effective}")
        return verdict(False, f"{found['place']}: {effective}, you accept {', '.join(allowed) or 'nothing'} there")
    if found["kind"] == "turkey":
        if effective in rules["turkey_modes"]:
            return verdict(True, f"Turkey ({location}): {effective}")
        return verdict(False, f"{location}: {effective}, you accept {', '.join(rules['turkey_modes']) or 'nothing'} elsewhere in Turkey")
    if found["kind"] == "none":
        return verdict(True, "no location given") if unknown_ok else verdict(False, "no location given")
    # abroad from here on: only when open to Turkey
    if restricted:
        return verdict(False, f"not open to Turkey: \"{restricted}\"")
    if found["kind"] == "abroad":
        return verdict(False, f"{location}: for that country, not open to Turkey")
    if found["kind"] == "bare_remote" and rules["bare_remote"] == "skip":
        return verdict(False, "remote, but no country or region named")
    if effective not in rules["abroad_modes"]:
        return verdict(False, f"{location}: {effective}, you accept {', '.join(rules['abroad_modes']) or 'nothing'} abroad")
    return verdict(True, f"{location}: open to Turkey, {effective}")
