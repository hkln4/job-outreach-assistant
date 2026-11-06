"""Sentence-level evidence for a letter (hallucination guard, letter side).

The metadata call maps every sentence of the finished letter to the facts it
states and, for each fact, a source (a fetched company page URL or an
evidence record E-number) plus the words in that source that say it. Code
then decides:
- a quote counts only if it is in THAT source's text (a company page, or the
  record's fields, code facts and owner-stated facts); a quote found on a
  different fetched page is accepted with the URL corrected;
- a company claim needs a verified page quote, an owner claim a verified
  record quote, "both" needs both;
- a sentence the model called "none" is still checked by code when it looks
  factual (a number, a technology name, the company's name, "your
  company"-style wording) - the model can't wave a claim through;
- letter sentences the model left out are unsupported.
Unsupported sentences are listed in "check before sending"; the letter is
never changed by this.
"""
import re

from app.applications.draft_review import quote_in_draft
from app.applications.grounding import numbers_in, technologies_in
from app.applications.synonyms import fold
from app.companies.context import locate_quote
from app.models import ProfileItem

MIN_QUOTE_WORDS = 2
COMPANY_WORDS_RE = re.compile(
    r"\b(şirketiniz|şirketinizin|firmanız|firmanızın|ekibiniz|ekibinizin|ürününüz|ürünleriniz|projeleriniz|"
    r"your (company|team|product|products|platform|work)|you (build|develop|make|offer|provide))\b",
    re.IGNORECASE,
)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def sentences(body: str) -> list[str]:
    """The body's sentences (3+ words), in order."""
    parts = [p.strip() for p in _SENTENCE_RE.split(body or "")]
    return [p for p in parts if len(re.findall(r"\w+", p)) >= 3]


def _normalized(text: str) -> str:
    text = (text or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", fold(text)).strip()


def item_text(item: ProfileItem) -> str:
    """Only the citable parts of a record count as a source (model framing
    such as role angles and talking points does not)."""
    from app.applications.evidence import citable_text

    return citable_text(item)


def _quote_in(quote: str, text: str) -> bool:
    needle = _normalized(quote).strip("\"'")
    return len(needle.split()) >= MIN_QUOTE_WORDS and needle in _normalized(text)


def looks_factual(sentence: str, company_name: str) -> bool:
    return bool(
        numbers_in(sentence)
        or technologies_in(sentence)
        or (company_name and fold(company_name) in fold(sentence))
        or COMPANY_WORDS_RE.search(sentence)
    )


def verify_support(support: dict, pages: list[dict], items: dict[int, ProfileItem]) -> dict | None:
    """The support with its kind ("page" | "record") and corrected source, or None."""
    source, quote = (support.get("source") or "").strip(), support.get("quote") or ""
    match = re.fullmatch(r"\[?E(\d+)\]?", source, re.IGNORECASE)
    if match:
        item = items.get(int(match.group(1)))
        if item is not None and _quote_in(quote, item_text(item)):
            return {"kind": "record", "source": f"E{item.id}", "quote": quote}
        # the quote may be in another offered record
        for other in items.values():
            if _quote_in(quote, item_text(other)):
                return {"kind": "record", "source": f"E{other.id}", "quote": quote}
        return None
    page = next((p for p in pages if p.get("url") == source), None)
    if page is not None and _quote_in(quote, page.get("text") or ""):
        return {"kind": "page", "source": source, "quote": quote}
    located = locate_quote(quote, pages) if len(_normalized(quote).split()) >= MIN_QUOTE_WORDS else None
    return {"kind": "page", "source": located, "quote": quote} if located else None


def check_claims(body: str, claims: list[dict], pages: list[dict], items: list[ProfileItem], company_name: str) -> dict:
    """{"checked": n, "supported": n, "unsupported": [{sentence, reason}], "sentences": [...]}."""
    by_id = {item.id: item for item in items}
    results = []
    for sentence in sentences(body):
        claim = next((c for c in claims if quote_in_draft(c.get("sentence", ""), "", sentence) or quote_in_draft(sentence, "", c.get("sentence", ""))), None)
        if claim is None:
            reason = "not_annotated" if looks_factual(sentence, company_name) else ""
            results.append({"sentence": sentence, "kind": "none", "supports": [], "reason": reason})
            continue
        kind = claim.get("kind") if claim.get("kind") in ("company", "owner", "both", "none") else "both"
        verified = [v for v in (verify_support(s, pages, by_id) for s in claim.get("supports") or []) if v]
        has_page = any(v["kind"] == "page" for v in verified)
        has_record = any(v["kind"] == "record" for v in verified)
        reason = ""
        if kind == "none":
            if looks_factual(sentence, company_name) and not verified:
                reason = "factual_without_source"
        elif kind == "company" and not has_page:
            reason = "company_claim_unverified"
        elif kind == "owner" and not has_record:
            reason = "owner_claim_unverified"
        elif kind == "both" and not (has_page and has_record):
            reason = "company_claim_unverified" if not has_page else "owner_claim_unverified"
        results.append({"sentence": sentence, "kind": kind, "supports": verified, "reason": reason})
    unsupported = [{"sentence": r["sentence"], "reason": r["reason"]} for r in results if r["reason"]]
    return {"checked": len(results), "supported": len(results) - len(unsupported), "unsupported": unsupported, "sentences": results}
