"""Technopark company directories -> [{name, website_url, sector, contact_email, brand_name}].

Built-in readers, one per directory, each reading the site's own structure
(no LLM needed):
- ODTÜ Teknokent: one published JSON file (the HTML page is filled in by
  JavaScript, so a plain fetch of the page sees nothing).
- Bilkent Cyberpark: paginated cards with name, sector and (often) website.
- Hacettepe Teknokent: sector categories -> company pages with website/email.
- OSTİM Teknopark: one list -> company pages with website/email and tags.
- Ankara Üniversitesi Teknokent: WordPress sitemap -> company pages.

Any other directory page the owner adds goes through the generic reader: the
page's text and outbound links go to the LLM, which lists the companies.

Whatever the reader, a website is only kept if the directory itself lists it
- names are never resolved to a site by guessing (CLAUDE.md section 11).
"""
import re
import ssl
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.companies.bulk_parse import domain_of
from app.companies.dedup import normalize_url
from app.email_send.gmail_client import validate_email
from app.companies.fetch import BROWSER_HEADERS
from app.llm.base import LLMProvider
from app.llm.json_utils import parse_json_object
from app.llm.untrusted import UNTRUSTED_RULE, wrap_untrusted

ODTU_TEKNOKENT_JSON = "https://odtuteknokent.com.tr/assets/data/companies.json"
CYBERPARK_ARCHIVE = "https://www.cyberpark.com.tr/firma-arsiv"
HACETTEPE_DIRECTORY = "https://www.hacettepeteknokent.com.tr/tr/firma_rehberi"
OSTIM_DIRECTORY = "https://ostimteknopark.com.tr/tr/firmalar-119"
ANKARA_TEKNOKENT_SITEMAP = "https://firmarehberi.ankarateknokent.com/wp-sitemap-posts-job_listing-1.xml"

MAX_CYBERPARK_PAGES = 60
# Company pages are fetched a few at a time - fast enough for a few hundred,
# gentle enough on a technopark's small web server.
DETAIL_WORKERS = 4
_SOCIAL_HOSTS = re.compile(
    r"linkedin|facebook|twitter|instagram|youtube|x\.com|google|wa\.me|whatsapp|wordpress|apple\.com", re.I
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

MAX_DIRECTORY_PAGES = 10
MAX_PAGE_TEXT_CHARS = 20000
MAX_LINKS_PER_PAGE = 400
# Below this much text across all pages the list is almost certainly rendered
# by JavaScript - say so instead of "found 0 companies".
MIN_DIRECTORY_TEXT_CHARS = 200

_PAGINATION_TEXT = re.compile(r"^(\d{1,3}|sonraki|ileri|next|»|›|>)$", re.IGNORECASE)
_PAGINATION_HREF = re.compile(r"[?&](page|sayfa|p|pg)=\d+", re.IGNORECASE)

DIRECTORY_SYSTEM_PROMPT = """You are reading one page of a technology park's company \
directory. List every company the page lists.

Rules:
- "name": the company name exactly as written on the page.
- "website": ONLY a URL copied from the LINKS list that clearly belongs to that company \
(its own site, not a social profile and not the directory itself). If there is no such \
link, use null. Never construct or guess a URL from the name.
- Skip navigation items, sponsors, news and anything that is not a listed company.
- Output ONLY valid JSON, this exact shape:
{"companies": [{"name": string, "website": string | null}]}
""" + UNTRUSTED_RULE


class DirectoryReadError(ValueError):
    """The directory couldn't be read - shown on the discovery page."""


def _client(timeout: float = 30) -> httpx.Client:
    """TLS is verified against the OS trust store: Bilkent Cyberpark serves an
    incomplete certificate chain that the OS completes and certifi can't."""
    try:
        import truststore

        verify: ssl.SSLContext | bool = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:  # pragma: no cover - listed in requirements.txt
        verify = True
    return httpx.Client(timeout=timeout, follow_redirects=True, headers=BROWSER_HEADERS, verify=verify)


def _entry(name: str, website: str | None = None, **extra) -> dict:
    email = (extra.get("contact_email") or "").strip()
    return {
        "name": " ".join(name.split()),
        "website_url": normalize_url(website),
        "sector": (extra.get("sector") or "").strip(),
        "contact_email": email if validate_email(email) else None,
        "brand_name": (extra.get("brand_name") or "").strip() or None,
    }


def _soup(client: httpx.Client, url: str) -> BeautifulSoup:
    resp = client.get(url)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def _company_links(soup: BeautifulSoup, page_url: str) -> list[str]:
    """Outbound links on a company page that aren't the park's own, social media,
    or an email typed into the website field ("https://team@4hera.com")."""
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"].strip())
        host = urlparse(href).netloc
        if (
            href.startswith(("http://", "https://"))
            and "@" not in host
            and domain_of(href) != domain_of(page_url)
            and not _SOCIAL_HOSTS.search(host)
        ):
            links.append(href)
    return links


def _decode_cfemail(hex_text: str) -> str:
    """Cloudflare's email obfuscation: first byte is the XOR key."""
    try:
        key = int(hex_text[:2], 16)
        return "".join(chr(int(hex_text[i : i + 2], 16) ^ key) for i in range(2, len(hex_text), 2))
    except ValueError:
        return ""


def _emails(soup: BeautifulSoup) -> list[str]:
    found = [_decode_cfemail(el["data-cfemail"]) for el in soup.select("[data-cfemail]")]
    found += [a["href"][7:].split("?")[0] for a in soup.find_all("a", href=True) if a["href"].startswith("mailto:")]
    found += _EMAIL_RE.findall(soup.get_text(" "))
    return [e for e in dict.fromkeys(found) if validate_email(e)]


def _fetch_details(client: httpx.Client, urls: list[str], parse) -> list[dict]:
    """parse(soup, url) -> {name, links, emails, sector?} | None for each
    company page; unreachable pages are skipped.

    Links and emails that show up on many company pages are the park's own
    site chrome (a menu link, the park's info@ address, the web agency in the
    footer) - never a company's. They're dropped before picking each
    company's website and email."""

    def one(url: str) -> dict | None:
        try:
            return parse(_soup(client, url), url)
        except (httpx.HTTPError, ValueError):
            return None

    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        raw = [item for item in pool.map(one, urls) if item and item.get("name")]

    seen_on: dict[str, int] = {}
    for item in raw:
        for value in {*(domain_of(link) for link in item["links"]), *item["emails"]}:
            seen_on[value] = seen_on.get(value, 0) + 1
    chrome_threshold = max(3, len(raw) // 20)
    entries = []
    for item in raw:
        links = [link for link in item["links"] if seen_on[domain_of(link)] < chrome_threshold]
        emails = [email for email in item["emails"] if seen_on[email] < chrome_threshold]
        entries.append(
            _entry(item["name"], links[0] if links else None, sector=item.get("sector", ""), contact_email=emails[0] if emails else None)
        )
    return entries


def _detail_fields(soup: BeautifulSoup, page_url: str, title: str) -> dict:
    main = soup.find("main") or soup
    links = _company_links(main, page_url)
    # Some pages print the website as plain text instead of a link.
    links += [
        normalize_url(match)
        for match in re.findall(r"(?:https?://|www\.)[^\s<>\"']+", main.get_text(" "))
        if domain_of(normalize_url(match)) != domain_of(page_url) and "@" not in match
    ]
    return {"name": title, "links": links, "emails": _emails(main)}


def _tr_title(text: str) -> str:
    """"BİLGİSAYAR VE İLETİŞİM" -> "Bilgisayar Ve İletişim" (str.title() mangles
    the Turkish dotted/dotless i)."""
    lower = text.replace("I", "ı").replace("İ", "i").lower()
    return " ".join(
        (word[0].replace("i", "İ").replace("ı", "I").upper() + word[1:]) if word else word for word in lower.split(" ")
    )


def _first_text(soup: BeautifulSoup, selector: str) -> str:
    el = soup.select_one(selector)
    return el.get_text(" ", strip=True) if el else ""


def read_odtu_json(url: str = ODTU_TEKNOKENT_JSON) -> list[dict]:
    with _client() as client:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, list):
        raise DirectoryReadError("The ODTÜ Teknokent directory file changed shape - expected a list.")
    entries = []
    for entry in data:
        name = (entry.get("name") or "").strip() if isinstance(entry, dict) else ""
        if name:
            entries.append(_entry(name, entry.get("url")))
    return entries


def read_cyberpark(url: str = CYBERPARK_ARCHIVE) -> list[dict]:
    """Cards: <h3 class="title">name</h3>, <p>sector</p>, and the card's own
    link is the company website when there is one. Pages /2, /3 ... until empty."""
    entries: list[dict] = []
    with _client() as client:
        for page in range(1, MAX_CYBERPARK_PAGES + 1):
            soup = _soup(client, url if page == 1 else f"{url}/{page}")
            cards = [col for col in soup.select("div.col-md-3") if col.select_one("h3.title")]
            if not cards:
                break
            for card in cards:
                link = card.find("a", href=True)
                sector = card.select_one("h3.title").find_next("p")
                entries.append(
                    _entry(
                        card.select_one("h3.title").get_text(" ", strip=True),
                        link["href"] if link else None,
                        sector=sector.get_text(" ", strip=True) if sector else "",
                    )
                )
    return entries


def read_hacettepe(url: str = HACETTEPE_DIRECTORY) -> list[dict]:
    """Sector categories -> company cards -> company pages (website + email as
    text). A company listed under several categories keeps all of them."""
    with _client() as client:
        index = _soup(client, url)
        categories = {
            urljoin(url, a["href"]): _tr_title(a.get_text(" ", strip=True))
            for a in index.find_all("a", href=True)
            if "/firma_rehberi/" in a["href"]
        }
        sectors: dict[str, list[str]] = {}
        for category_url, category in categories.items():
            try:
                page = _soup(client, category_url)
            except httpx.HTTPError:
                continue
            for a in page.select("div.firma_adi a[href]"):
                sectors.setdefault(urljoin(category_url, a["href"]), []).append(category)

        def parse(soup: BeautifulSoup, page_url: str) -> dict:
            # Only the contact block - the free-text description below it has
            # its own headings and links that aren't the company's.
            info = BeautifulSoup("".join(str(el) for el in soup.select("div.firma_detay_bilgi")), "html.parser")
            fields = _detail_fields(info, page_url, _first_text(soup, "div.firma_detay_baslik"))
            return {**fields, "sector": ", ".join(sectors[page_url])}

        return _fetch_details(client, list(sectors), parse)


def read_ostim(url: str = OSTIM_DIRECTORY) -> list[dict]:
    """One list page -> company pages with website (when given), email and tags."""
    with _client() as client:
        index = _soup(client, url)
        pages = list(
            dict.fromkeys(urljoin(url, a["href"]) for a in index.find_all("a", href=True) if "/company/" in a["href"])
        )

        def parse(soup: BeautifulSoup, page_url: str) -> dict:
            return _detail_fields(soup, page_url, _first_text(soup, "h1") or _first_text(soup, "h2"))

        return _fetch_details(client, pages, parse)


def read_ankara_teknokent(url: str = ANKARA_TEKNOKENT_SITEMAP) -> list[dict]:
    """The directory is a WordPress listing site rendered by JavaScript; its
    sitemap lists every company page, and each page carries the brand name
    and the website link."""
    with _client() as client:
        resp = client.get(url)
        resp.raise_for_status()
        pages = re.findall(r"<loc>([^<]+)</loc>", resp.text)

        def parse(soup: BeautifulSoup, page_url: str) -> dict:
            return _detail_fields(soup, page_url, _first_text(soup, "h1"))

        return _fetch_details(client, pages, parse)


def _page_text_and_links(html: str, page_url: str) -> tuple[str, list[tuple[str, str]], list[str]]:
    """Visible text, outbound (company) links, and same-site pagination links."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)

    outbound: list[tuple[str, str]] = []
    pages: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urldefrag(urljoin(page_url, href))[0]
        if absolute in seen or urlparse(absolute).scheme not in ("http", "https"):
            continue
        seen.add(absolute)
        label = " ".join(a.get_text(" ", strip=True).split())
        if domain_of(absolute) != domain_of(page_url):
            outbound.append((label, absolute))
        elif _PAGINATION_TEXT.match(label) or _PAGINATION_HREF.search(absolute):
            pages.append(absolute)
    return text, outbound[:MAX_LINKS_PER_PAGE], pages


def _companies_on_page(llm: LLMProvider, text: str, links: list[tuple[str, str]]) -> list[dict]:
    link_lines = "\n".join(f"- {label or '(no text)'} -> {href}" for label, href in links) or "none"
    prompt = (
        f"PAGE TEXT:\n{wrap_untrusted(text[:MAX_PAGE_TEXT_CHARS], label='DIRECTORY PAGE')}\n\n"
        f"LINKS:\n{wrap_untrusted(link_lines, label='DIRECTORY LINKS')}"
    )
    response = llm.generate(prompt=prompt, system=DIRECTORY_SYSTEM_PROMPT, temperature=0.0, max_output_tokens=8000)
    data = parse_json_object(response.text, fallback={"companies": []})
    if "parse_error" in data:
        raise DirectoryReadError("Reading the directory page produced invalid output - try again.")

    allowed = {href.rstrip("/").lower(): href for _, href in links}
    companies = []
    for entry in data.get("companies") or []:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        website = (entry.get("website") or "").strip()
        if not name:
            continue
        # Evidence rule: a website only counts if it was literally linked on the page.
        companies.append(_entry(name, allowed.get(website.rstrip("/").lower())))
    return companies


def read_html_directory(llm: LLMProvider, url: str) -> list[dict]:
    queue = [url]
    visited: set[str] = set()
    total_text = 0
    companies: list[dict] = []
    with _client(timeout=20) as client:
        while queue and len(visited) < MAX_DIRECTORY_PAGES:
            page_url = queue.pop(0)
            if page_url in visited:
                continue
            visited.add(page_url)
            try:
                resp = client.get(page_url)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                if page_url == url:
                    raise DirectoryReadError(f"Couldn't fetch the directory page: {exc}") from exc
                continue
            text, links, pages = _page_text_and_links(resp.text, page_url)
            total_text += len(text)
            if text.strip():
                companies.extend(_companies_on_page(llm, text, links))
            queue.extend(p for p in pages if p not in visited and p not in queue)

    if total_text < MIN_DIRECTORY_TEXT_CHARS:
        raise DirectoryReadError(
            "The page has almost no text - the list is probably loaded by JavaScript, "
            "which a plain fetch can't see. Look for the data file the page loads instead."
        )
    return companies


READERS = {
    "odtu_json": read_odtu_json,
    "cyberpark": read_cyberpark,
    "hacettepe": read_hacettepe,
    "ostim": read_ostim,
    "ankara_teknokent": read_ankara_teknokent,
}


def read_source(llm: LLMProvider, kind: str, url: str) -> list[dict]:
    reader = READERS.get(kind)
    entries = reader(url) if reader else read_html_directory(llm, url)
    if not entries:
        raise DirectoryReadError("No companies were found - the directory's layout may have changed.")
    return entries
