"""The owner's sector filter for discovery, applied by code (no LLM).

It reads what the app already has for free: the directory's own sector
field and the homepage text the liveness check downloaded. A candidate is
- "match": a target word appears (and no excluded word without one),
- "off": an excluded word appears and no target word, or the homepage was
  read and names no target word at all,
- "unclear": nothing to read yet (no site, or not checked).

Off-sector candidates are never sent to AI screening by default; the owner
rejects them in one click (or looks first).
"""
import re

from app.applications.synonyms import fold

DEFAULT_SECTOR_FILTER = {
    "include": [
        "yapay zeka", "artificial intelligence", "machine learning", "makine ogrenmesi", "deep learning", "derin ogrenme",
        "computer vision", "goruntu isleme", "bilgisayarli goru", "image processing", "veri bilimi", "data science",
        "big data", "buyuk veri", "analytics", "analitik", "yazilim", "software", "saas", "cloud", "bulut", "platform",
        "nlp", "dogal dil", "robotik", "robotics", "otonom", "autonomous", "iot", "siber guvenlik", "cyber security",
    ],
    "exclude": [
        "insaat", "construction", "gida", "food", "tekstil", "textile", "mobilya", "furniture", "emlak", "real estate",
        "kozmetik", "cosmetics", "turizm", "tourism", "otel", "restoran", "matbaa", "ambalaj", "packaging", "tarim",
        "hukuk burosu", "law firm", "muhasebe burosu", "insan kaynaklari danismanligi",
    ],
}
TEXT_KEEP = 4000  # characters of homepage text kept from the liveness check


def normalized(raw: dict | None) -> dict:
    data = {**DEFAULT_SECTOR_FILTER, **(raw or {})}
    data["include"] = [w.strip() for w in data.get("include") or [] if w.strip()]
    data["exclude"] = [w.strip() for w in data.get("exclude") or [] if w.strip()]
    return data


def _find(words: list[str], text: str) -> list[tuple[str, str]]:
    """[(word, snippet)] for the words found in the folded text."""
    hits = []
    for word in words:
        match = re.search(rf"(?<!\w){re.escape(fold(word))}(?!\w)", text)
        if match:
            start = max(0, match.start() - 50)
            hits.append((word, text[start : match.end() + 50].strip()))
    return hits


def sector_check(rules: dict, directory_sector: str = "", homepage_text: str = "") -> dict:
    """{"result": match | off | unclear, "hits": [...], "excluded": [...], "evidence": snippet}."""
    rules = normalized(rules)
    text = fold(f"{directory_sector}\n{homepage_text}")
    included = _find(rules["include"], text)
    excluded = _find(rules["exclude"], text)
    if included:
        return {"result": "match", "hits": [w for w, _ in included], "excluded": [w for w, _ in excluded], "evidence": included[0][1]}
    if excluded:
        return {"result": "off", "hits": [], "excluded": [w for w, _ in excluded], "evidence": excluded[0][1]}
    if homepage_text.strip():
        return {"result": "off", "hits": [], "excluded": [], "evidence": "none of your sector words on the homepage"}
    return {"result": "unclear", "hits": [], "excluded": [], "evidence": ""}


def candidate_sector(rules: dict, candidate) -> dict:
    return sector_check(rules, candidate.sector or "", (candidate.liveness or {}).get("text", ""))


def owner_sector_filter(db) -> dict:
    from app.models import OwnerSettings

    settings = db.query(OwnerSettings).first()
    return normalized(settings.discovery_filter if settings else None)
