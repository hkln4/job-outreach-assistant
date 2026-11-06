"""Company text for a prompt, allocated by priority (CLAUDE.md section 8).

Several call sites send all researched pages to a model. Joining them and
slicing `text[:N]` cut whatever came last - the job-posting pages, which are
the most useful text for a letter and a likely place for a dealbreaker. Here
each page gets a share of the budget instead (weighted water-filling: a short
page donates what it doesn't need to the others), pages are emitted most
decision-relevant first (job postings, careers, about, home, contact), and
every page keeps its `[url]` header so sources can still be validated.
"""
import re

from app.companies.fetch import _trim_keep_head_and_tail

KIND_ORDER = ("job", "careers", "about", "home", "other", "contact")
KIND_WEIGHT = {"job": 1.5, "careers": 1.25}
SEPARATOR = "\n\n---\n\n"
# Share of the budget the research summary + open positions may take.
HEADER_SHARE = 0.2

_URL_HINTS = (
    ("careers", ("career", "kariyer", "jobs", "insan-kaynak", "join")),
    ("contact", ("contact", "iletisim", "iletişim")),
    ("about", ("about", "hakkimizda", "hakkımızda", "kurumsal")),
)


def page_kind(page: dict, index: int) -> str:
    """The page's kind as research recorded it; for research saved before
    kinds existed, inferred from position (first page is home) and URL."""
    if page.get("kind") in KIND_ORDER:
        return page["kind"]
    if index == 0:
        return "home"
    url = (page.get("url") or "").lower()
    for kind, hints in _URL_HINTS:
        if any(hint in url for hint in hints):
            return kind
    return "other"


def ordered_pages(pages: list[dict]) -> list[dict]:
    """Pages with their kind, most decision-relevant first (stable)."""
    tagged = [{**page, "kind": page_kind(page, i)} for i, page in enumerate(pages)]
    return sorted(tagged, key=lambda page: KIND_ORDER.index(page["kind"]))


def _allocate(needs: list[int], weights: list[float], budget: int) -> list[int]:
    """Weighted water-filling: find a level L so that sum(min(need, w*L)) fits."""
    if sum(needs) <= budget:
        return list(needs)
    low, high = 0.0, float(max(needs))
    for _ in range(40):
        level = (low + high) / 2
        if sum(min(need, weight * level) for need, weight in zip(needs, weights)) > budget:
            high = level
        else:
            low = level
    return [int(min(need, weight * low)) for need, weight in zip(needs, weights)]


def _header(summary_verified: list[dict], open_positions: list[dict] | None) -> str:
    """Research notes for a prompt - only what code verified: summary
    sentences backed by a quote on a page, and positions whose title is on a
    page. The model-written summary itself never reaches a later prompt as a
    fact (the pages below are the source)."""
    lines = []
    if summary_verified:
        lines.append("RESEARCH NOTES (each backed by a quote found on the site):")
        lines += [f"- {entry['sentence']} [{entry['url']}]" for entry in summary_verified]
    positions = [p for p in open_positions or [] if p.get("verified")]
    if positions:
        lines.append("OPEN POSITIONS (titles found on the site):")
        for position in positions:
            title = (position.get("title") or "").strip()
            details = (position.get("details") or "").strip()
            lines.append(f"- {title}: {details} [{position.get('url', '')}]" if details else f"- {title} [{position.get('url', '')}]")
    return "\n".join(lines)


def build_company_context(
    pages: list[dict],
    budget: int,
    *,
    summary_verified: list[dict] | None = None,
    open_positions: list[dict] | None = None,
) -> str:
    """Pages joined as `[url]\\ntext` blocks within `budget` characters,
    optionally prefixed by the research summary and open positions."""
    parts: list[str] = []
    header = _header(summary_verified or [], open_positions)
    if header:
        header = header[: int(budget * HEADER_SHARE)]
        parts.append(header)
        budget -= len(header) + len(SEPARATOR)

    ordered = [page for page in ordered_pages(pages) if (page.get("text") or "").strip()]
    if ordered:
        labels = [f"[{page['url']}]\n" for page in ordered]
        overhead = sum(len(label) for label in labels) + len(SEPARATOR) * len(ordered)
        available = max(0, budget - overhead)
        needs = [len(page["text"]) for page in ordered]
        weights = [KIND_WEIGHT.get(page["kind"], 1.0) for page in ordered]
        for label, page, share in zip(labels, ordered, _allocate(needs, weights, available)):
            if share <= 0:
                continue
            parts.append(label + _trim_keep_head_and_tail(page["text"], share))
    return SEPARATOR.join(parts)


def research_context(research: dict, budget: int, *, with_header: bool = False) -> str:
    research = research or {}
    return build_company_context(
        research.get("pages") or [],
        budget,
        summary_verified=research.get("summary_verified") if with_header else None,
        open_positions=research.get("open_positions") if with_header else None,
    )


def _normalize(text: str) -> str:
    text = (text or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip().casefold()


def locate_quote(quote: str, pages: list[dict]) -> str | None:
    """URL of the first page whose text contains `quote` (whitespace-
    normalized, casefolded). A quote the model shortened with "..." counts
    when its longest piece is found. None when no page has it."""
    needle = _normalize(quote).strip("\"'")
    if len(needle) < 3:
        return None
    pieces = [piece.strip() for piece in needle.replace("…", "...").split("...") if piece.strip()]
    longest = max(pieces, key=len) if pieces else needle
    for page in pages:
        haystack = _normalize(page.get("text") or "")
        if needle in haystack or (len(longest) >= 12 and longest in haystack):
            return page.get("url")
    return None
