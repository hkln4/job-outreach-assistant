"""Site + careers page fetch and text extraction (CLAUDE.md section 5 step 2).
Retrieval stays in the app - the model never gets its own web-search/browsing tool.

Fetches up to a handful of pages (home, careers, about, contact) instead of just
the homepage, since a single homepage is often thin marketing copy with no real
company info, no jobs, and no contact email - see MAX_CHARS_PER_PAGE below for how
each page's share of the LLM's context budget is kept bounded as more pages are added.
"""
from urllib.parse import urldefrag, urljoin

import httpx
from bs4 import BeautifulSoup
from readability import Document as ReadabilityDocument

MAX_CHARS_PER_PAGE = 6000
# Contact info (email, address) is disproportionately likely to be in a page's
# footer, i.e. right at the end - a naive head-only truncation on a long page
# would systematically cut it. Keep most of the budget for the head, but always
# keep a tail slice too.
HEAD_SHARE = 0.7
MIN_READABILITY_CHARS = 400
MAX_PAGES = 4

# Strong: unambiguous signals. Weak: generic CTAs that often show up on unrelated
# content (e.g. "join us" on a partners/community page) - only trusted if nothing
# strong is found, and even then only from the anchor TEXT, not the href (an href
# like "/partners/join-us" would otherwise false-match "join-us").
CAREER_KEYWORDS_STRONG = ["career", "careers", "jobs", "job", "kariyer"]
CAREER_KEYWORDS_WEAK = ["join us", "join-us", "join our team"]
ABOUT_KEYWORDS_STRONG = ["about-us", "about us", "hakkımızda", "kurumsal", "who we are"]
ABOUT_KEYWORDS_WEAK = ["about", "company"]
CONTACT_KEYWORDS_STRONG = ["contact-us", "contact us", "iletişim", "bize ulaşın"]
CONTACT_KEYWORDS_WEAK = ["contact"]


def _trim_keep_head_and_tail(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head_len = int(max_chars * HEAD_SHARE)
    tail_len = max_chars - head_len
    return text[:head_len] + "\n...\n" + text[-tail_len:]


def _extract_main_text(html: str) -> str:
    doc = ReadabilityDocument(html)
    soup = BeautifulSoup(doc.summary(), "html.parser")
    text = soup.get_text("\n", strip=True)
    if len(text) >= MIN_READABILITY_CHARS:
        return text

    # Readability's "main article" heuristic is tuned for blog/news pages and
    # regularly picks a single small card out of marketing/corporate homepages,
    # discarding most of the real page (and anything - like a contact email -
    # that isn't inside that one card). Fall back to the full page's text.
    full_soup = BeautifulSoup(html, "html.parser")
    for tag in full_soup(["script", "style", "noscript"]):
        tag.decompose()
    full_text = full_soup.get_text("\n", strip=True)
    return full_text if len(full_text) > len(text) else text


def _find_link(html: str, base_url: str, strong_keywords: list[str], weak_keywords: list[str]) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    links = soup.find_all("a", href=True)

    for a in links:
        label = (a.get_text() or "").lower()
        href = a["href"].lower()
        if any(kw in label or kw in href for kw in strong_keywords):
            return urljoin(base_url, a["href"])

    for a in links:
        label = (a.get_text() or "").lower()
        if any(kw in label for kw in weak_keywords):
            return urljoin(base_url, a["href"])

    return None


def _find_careers_link(html: str, base_url: str) -> str | None:
    return _find_link(html, base_url, CAREER_KEYWORDS_STRONG, CAREER_KEYWORDS_WEAK)


def fetch_company_research(website_url: str, careers_url: str | None = None) -> dict:
    """Returns {"pages": [{"url", "text"}], "detected_careers_url": str | None}.

    Always fetches the homepage. Then, on top of the (optionally owner-provided)
    careers page, also looks for an About and a Contact page - About usually has
    the real company description a thin homepage lacks, and Contact is often
    where an application email actually lives. Capped at MAX_PAGES total.
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ApplicationOutreachAssistant/1.0)"}
    pages = []
    seen_urls = {urldefrag(website_url)[0].rstrip("/")}

    with httpx.Client(timeout=15, follow_redirects=True, headers=headers) as client:
        home_resp = client.get(website_url)
        home_resp.raise_for_status()
        home_html = home_resp.text
        pages.append(
            {"url": website_url, "text": _trim_keep_head_and_tail(_extract_main_text(home_html), MAX_CHARS_PER_PAGE)}
        )

        detected_careers = careers_url or _find_careers_link(home_html, website_url)

        candidate_urls = [detected_careers] if detected_careers else []
        candidate_urls.append(_find_link(home_html, website_url, ABOUT_KEYWORDS_STRONG, ABOUT_KEYWORDS_WEAK))
        candidate_urls.append(_find_link(home_html, website_url, CONTACT_KEYWORDS_STRONG, CONTACT_KEYWORDS_WEAK))

        for url in candidate_urls:
            if not url or len(pages) >= MAX_PAGES:
                continue
            normalized = urldefrag(url)[0].rstrip("/")
            if normalized in seen_urls:
                continue  # same page as one already fetched (e.g. a "#contact" anchor on the homepage)
            seen_urls.add(normalized)
            try:
                resp = client.get(url)
                resp.raise_for_status()
                pages.append(
                    {"url": url, "text": _trim_keep_head_and_tail(_extract_main_text(resp.text), MAX_CHARS_PER_PAGE)}
                )
            except httpx.HTTPError:
                pass

    return {"pages": pages, "detected_careers_url": detected_careers}
