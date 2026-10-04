"""Site + careers page fetch and text extraction (CLAUDE.md section 5 step 2).
Retrieval stays in the app - the model never gets its own web-search/browsing tool.

Fetches up to a handful of pages (home, careers, about, contact) instead of just
the homepage, since a single homepage is often thin marketing copy with no real
company info, no jobs, and no contact email - see MAX_CHARS_PER_PAGE below for how
each page's share of the LLM's context budget is kept bounded as more pages are added.
"""
import re
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from readability import Document as ReadabilityDocument

# Ordinary browser headers, not a bot UA: several perfectly public company
# sites (WAF defaults) 403 anything that self-identifies as a tool. This is
# the owner's own outreach assistant fetching public pages they're entitled
# to read (CLAUDE.md section 9) - sites with real bot protection still 403
# and that failure stays visible on the company page.
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,tr;q=0.8",
}

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

# Individual job-posting links on the careers page get followed one level deep
# (capped), so research sees actual requirements/stack instead of only a list of
# titles. Same-site links only - external portals (kariyer.net, LinkedIn, ATS
# sites) are reported by the LLM as a portal, not crawled.
MAX_JOB_PAGES = 3
JOB_LINK_KEYWORDS = [
    "job", "position", "opening", "vacancy", "apply", "ilan", "basvur", "başvur",
    "engineer", "developer", "specialist", "scientist", "analyst", "designer",
    "architect", "manager", "intern", "mühendis", "muhendis", "uzman", "yazılım",
    "yazilim", "stajyer", "gelistirici", "geliştirici",
]
# A careers page whose extracted text is this thin is almost certainly rendered
# by JavaScript (plain HTTP fetch sees an empty shell) - flagged so the owner
# knows to check the page in a browser themselves.
THIN_CAREERS_CHARS = 300

# Below this much text across ALL fetched pages there is nothing to research
# with: the LLM would be asked to describe a company from a blank page and would
# either invent something or refuse. Flagged so drafting can be blocked and the
# owner told to fix the URL instead of getting a confusing failure later.
MIN_USABLE_RESEARCH_CHARS = 200

# Some sites serve a near-empty shell whose only job is a JavaScript redirect to
# a language path ("/tr", "/en"). A plain HTTP fetch stops there and extracts
# nothing, so when a page comes back empty we look inside its scripts for the
# paths they would have sent a browser to, and try those.
MAX_REDIRECT_CANDIDATES = 3
_JS_REDIRECT_MARKERS = ("window.location", "location.href", "location.replace")
_QUOTED_RE = re.compile(r"""['"]([^'"\s<>]{1,40})['"]""")


def _same_site(url: str, site_url: str) -> bool:
    a = urlparse(url).netloc.lower().removeprefix("www.")
    b = urlparse(site_url).netloc.lower().removeprefix("www.")
    return a == b


def _find_job_links(careers_html: str, careers_url: str, site_url: str) -> list[str]:
    """Links on the careers page that look like individual job postings."""
    soup = BeautifulSoup(careers_html, "html.parser")
    found: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not _is_fetchable(href) or href.lower().endswith(".pdf"):
            continue
        url = urldefrag(urljoin(careers_url, href))[0].rstrip("/")
        if url in seen or not _same_site(url, site_url) or url == urldefrag(careers_url)[0].rstrip("/"):
            continue
        label = (a.get_text() or "").lower()
        target = label + " " + url.lower()
        if any(kw in target for kw in JOB_LINK_KEYWORDS):
            seen.add(url)
            found.append(url)
    return found


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


def _is_fetchable(href: str) -> bool:
    """Menus are full of javascript:;/mailto:/tel: pseudo-links whose labels
    ("Contact", "Company") match our keywords - resolving one of those produced
    an unfetchable URL and crashed research (e.g. "unknown url type: '/;'")."""
    return not href.startswith(("javascript:", "mailto:", "tel:", "#"))


def _find_link(html: str, base_url: str, strong_keywords: list[str], weak_keywords: list[str]) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    links = [a for a in soup.find_all("a", href=True) if _is_fetchable(a["href"])]

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


def _normalize_linkedin_company_url(href: str) -> str | None:
    """A company LinkedIn URL reduced to its canonical form, or None if `href`
    isn't one. Company links live in header/footer social icons, so they're read
    from the raw HTML - readability drops those regions entirely.

    Sub-pages ("/company/acme/about/") collapse to the company root, locale
    hosts ("tr.linkedin.com") to www, and tracking query strings are dropped.
    Only /company/ pages count - personal profiles (/in/), share widgets and
    job links are not the company page the LinkedIn research tools need.
    """
    parsed = urlparse(href)
    host = parsed.netloc.lower()
    if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2 or parts[0].lower() != "company":
        return None
    slug = parts[1]
    if slug.lower() in ("unavailable", "setup"):
        return None
    return f"https://www.linkedin.com/company/{slug}"


def _js_redirect_candidates(html: str, base_url: str) -> list[str]:
    """URLs a JS-redirecting shell page would have sent a browser to.

    Only worth trying when a page extracted to nothing. Two shapes cover the
    common language-splash case: an explicit path ("/tr/") and a list of
    language codes the script appends to "/" (var a=['en','tr']).
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = "\n".join(s.get_text() or "" for s in soup.find_all("script"))
    if not any(marker in scripts for marker in _JS_REDIRECT_MARKERS):
        return []

    candidates: list[str] = []
    seen: set[str] = set()
    for token in _QUOTED_RE.findall(scripts):
        if token.startswith("/") and not token.startswith("//"):
            path = token
        elif token.isalpha() and 2 <= len(token) <= 5:
            path = "/" + token  # language code the script appends to "/"
        else:
            continue
        url = urljoin(base_url, path)
        if url not in seen and url.rstrip("/") != base_url.rstrip("/"):
            seen.add(url)
            candidates.append(url)
        if len(candidates) >= MAX_REDIRECT_CANDIDATES:
            break
    return candidates


def _find_linkedin_link(html: str, base_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        candidate = _normalize_linkedin_company_url(urljoin(base_url, a["href"]))
        if candidate:
            return candidate
    return None


def find_linkedin_url(website_url: str) -> str | None:
    """The company's LinkedIn page, from the link on their own site.

    Deliberately far cheaper than fetch_company_research: no text extraction,
    no LLM, no LinkedIn traffic - just the homepage, and the contact/about page
    only if the homepage has no social icons. This is what fills in the
    LinkedIn URL for companies researched before LinkedIn research existed,
    without paying for a full re-research.
    """
    if not website_url:
        return None
    with httpx.Client(timeout=15, follow_redirects=True, headers=BROWSER_HEADERS) as client:
        try:
            home = client.get(website_url)
            home.raise_for_status()
        except httpx.HTTPError:
            return None

        found = _find_linkedin_link(home.text, website_url)
        if found:
            return found

        for url in (
            _find_link(home.text, website_url, CONTACT_KEYWORDS_STRONG, CONTACT_KEYWORDS_WEAK),
            _find_link(home.text, website_url, ABOUT_KEYWORDS_STRONG, ABOUT_KEYWORDS_WEAK),
        ):
            if not url or not url.startswith(("http://", "https://")):
                continue
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError:
                continue
            found = _find_linkedin_link(resp.text, url)
            if found:
                return found
    return None


def fetch_company_research(website_url: str, careers_url: str | None = None) -> dict:
    """Returns {"pages": [{"url", "text"}], "detected_careers_url": str | None,
    "careers_page_thin": bool, "detected_linkedin_url": str | None,
    "content_chars": int}.

    Always fetches the homepage. Then, on top of the (optionally owner-provided)
    careers page, also looks for an About and a Contact page - About usually has
    the real company description a thin homepage lacks, and Contact is often
    where an application email actually lives. Core pages are capped at
    MAX_PAGES; on top of that, up to MAX_JOB_PAGES individual job-posting links
    found on the careers page are followed so research sees actual
    requirements, not just titles.
    """
    pages = []
    # (url, raw html) of every page fetched, kept only for link discovery -
    # what goes to the LLM is always the extracted text in `pages`.
    fetched_html: list[tuple[str, str]] = []
    seen_urls = {urldefrag(website_url)[0].rstrip("/")}

    with httpx.Client(timeout=15, follow_redirects=True, headers=BROWSER_HEADERS) as client:

        def fetch_page(url: str) -> str | None:
            """Fetch url into `pages` (deduped); returns the raw HTML, or None."""
            if not url.startswith(("http://", "https://")):
                return None  # last-resort guard against unfetchable link schemes
            normalized = urldefrag(url)[0].rstrip("/")
            if normalized in seen_urls:
                return None  # same page as one already fetched (e.g. a "#contact" anchor)
            seen_urls.add(normalized)
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError:
                return None
            pages.append(
                {"url": url, "text": _trim_keep_head_and_tail(_extract_main_text(resp.text), MAX_CHARS_PER_PAGE)}
            )
            fetched_html.append((url, resp.text))
            return resp.text

        home_resp = client.get(website_url)
        home_resp.raise_for_status()
        home_html = home_resp.text
        home_text = _extract_main_text(home_html)

        # A homepage that is only a JavaScript language-splash extracts to
        # nothing. Follow where it would have sent a browser, and research that
        # page as the homepage instead of "researching" a blank shell.
        if len(home_text) < MIN_USABLE_RESEARCH_CHARS:
            for candidate in _js_redirect_candidates(home_html, website_url):
                try:
                    resp = client.get(candidate)
                    resp.raise_for_status()
                except httpx.HTTPError:
                    continue
                candidate_text = _extract_main_text(resp.text)
                if len(candidate_text) > len(home_text):
                    website_url, home_html, home_text = candidate, resp.text, candidate_text
                    seen_urls.add(urldefrag(candidate)[0].rstrip("/"))
                    break

        pages.append({"url": website_url, "text": _trim_keep_head_and_tail(home_text, MAX_CHARS_PER_PAGE)})
        fetched_html.append((website_url, home_html))

        detected_careers = careers_url or _find_careers_link(home_html, website_url)

        careers_html = None
        careers_page_thin = False
        if detected_careers:
            careers_html = fetch_page(detected_careers)
            if careers_html is not None:
                careers_page_thin = len(pages[-1]["text"]) < THIN_CAREERS_CHARS

        for url in (
            _find_link(home_html, website_url, ABOUT_KEYWORDS_STRONG, ABOUT_KEYWORDS_WEAK),
            _find_link(home_html, website_url, CONTACT_KEYWORDS_STRONG, CONTACT_KEYWORDS_WEAK),
        ):
            if url and len(pages) < MAX_PAGES:
                fetch_page(url)

        if careers_html:
            core_page_count = len(pages)
            for url in _find_job_links(careers_html, detected_careers, website_url):
                if len(pages) >= core_page_count + MAX_JOB_PAGES:
                    break
                fetch_page(url)

    detected_linkedin = None
    for page_url, html in fetched_html:
        detected_linkedin = _find_linkedin_link(html, page_url)
        if detected_linkedin:
            break

    return {
        "pages": pages,
        "detected_careers_url": detected_careers,
        "careers_page_thin": careers_page_thin,
        "detected_linkedin_url": detected_linkedin,
        # How much text the LLM will actually get. Zero happens (JS-only sites,
        # shells behind a WAF) and must not be mistaken for "researched".
        "content_chars": sum(len(p["text"]) for p in pages),
    }
