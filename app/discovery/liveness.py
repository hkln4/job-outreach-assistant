"""Liveness check for directory companies (roadmap phase 2) - no LLM.

Many technopark listings are dead sites, parked domains or shells untouched
for years. This filters them with plain HTTP before any model call:

- DNS resolves; HTTP status; TLS valid; the final URL after redirects (a
  different registrable domain means acquired or renamed);
- parked / placeholder pages ("domain for sale", "yapım aşamasında", default
  server pages);
- freshness: the newest year in a copyright line, the newest `lastmod` in
  /sitemap.xml (or the newest child of a sitemap index), the Last-Modified
  header, and the newest date on a blog/news page linked from home;
- extracted text length, and whether a careers page exists.

Status: DEAD (no DNS / connection failure / 4xx-5xx / parked), STALE (newest
freshness signal older than LIVENESS_STALE_YEARS), UNKNOWN (no freshness
signal at all), ALIVE otherwise. Every signal carries its evidence and URL.
"""
import re
import socket
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.channels.domains import host_of, registrable_domain
from app.companies.fetch import BROWSER_HEADERS, _extract_main_text, _find_careers_link, _find_link, _js_redirect_candidates
from app.config import get_settings

PARKED_PATTERNS = (
    "domain for sale", "this domain is for sale", "buy this domain", "domain is parked", "parked free",
    "bu alan adı satılıktır", "bu alan adi satiliktir", "alan adı satılık", "satılık alan adı",
    "yapım aşamasında", "yapim asamasinda", "under construction", "coming soon", "çok yakında", "cok yakinda",
    "website is under maintenance", "site bakımda",
    "welcome to nginx", "apache2 ubuntu default page", "it works!", "iis windows server", "default web site page",
    "cpanel", "plesk", "index of /", "sedo domain",
)
COPYRIGHT_RE = re.compile(r"(?:©|&copy;|\(c\)|copyright|telif)[^0-9]{0,30}((?:19|20)\d{2})(?:\s*[-–]\s*((?:19|20)\d{2}))?", re.IGNORECASE)
ISO_DATE_RE = re.compile(r"\b((?:19|20)\d{2})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b")
TR_DATE_RE = re.compile(r"\b(0?[1-9]|[12]\d|3[01])[./](0?[1-9]|1[0-2])[./]((?:19|20)\d{2})\b")
MONTHS = {
    "ocak": 1, "şubat": 2, "subat": 2, "mart": 3, "nisan": 4, "mayıs": 5, "mayis": 5, "haziran": 6, "temmuz": 7,
    "ağustos": 8, "agustos": 8, "eylül": 9, "eylul": 9, "ekim": 10, "kasım": 11, "kasim": 11, "aralık": 12, "aralik": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
WORD_DATE_RE = re.compile(r"\b(\d{1,2})\s+([A-Za-zÇĞİÖŞÜçğıöşü]+)\s+((?:19|20)\d{2})\b|\b([A-Za-z]+)\s+(\d{1,2}),\s*((?:19|20)\d{2})\b")
NEWS_KEYWORDS = ["blog", "news", "haber", "duyuru", "basın", "basin", "press", "etkinlik", "events"]
MIN_TEXT_CHARS = 200
TIMEOUT = 12


def _signal(name: str, value, evidence: str = "", url: str = "") -> dict:
    return {"name": name, "value": value, "evidence": str(evidence)[:200], "url": url}


def _dates_in(text: str, now: datetime) -> list[datetime]:
    """Plausible dates in free text (never in the future)."""
    found = []
    for y, m, d in ISO_DATE_RE.findall(text):
        found.append((int(y), int(m), int(d)))
    for d, m, y in TR_DATE_RE.findall(text):
        found.append((int(y), int(m), int(d)))
    for d1, mon1, y1, mon2, d2, y2 in WORD_DATE_RE.findall(text):
        month = MONTHS.get((mon1 or mon2).casefold())
        if month:
            found.append((int(y1 or y2), month, int(d1 or d2)))
    dates = []
    for y, m, d in found:
        try:
            value = datetime(y, m, min(d, 28), tzinfo=timezone.utc)
        except ValueError:
            continue
        if value <= now:
            dates.append(value)
    return dates


def _copyright_year(html: str, now: datetime) -> int | None:
    years = []
    for start, end in COPYRIGHT_RE.findall(html):
        for year in (start, end):
            if year and int(year) <= now.year:
                years.append(int(year))
    return max(years) if years else None


def _sitemap_lastmod(client: httpx.Client, base: str, now: datetime) -> tuple[datetime | None, str]:
    url = urljoin(base, "/sitemap.xml")
    try:
        resp = client.get(url)
        if resp.status_code >= 400:
            return None, url
    except httpx.HTTPError:
        return None, url
    soup = BeautifulSoup(resp.text, "html.parser")
    lastmods = [m.get_text(strip=True) for m in soup.find_all("lastmod")]
    if soup.find("sitemapindex") and not lastmods:
        children = [loc.get_text(strip=True) for loc in soup.find_all("loc")][:2]
        for child in children:
            try:
                child_resp = client.get(child)
                lastmods += [m.get_text(strip=True) for m in BeautifulSoup(child_resp.text, "html.parser").find_all("lastmod")]
            except httpx.HTTPError:
                continue
    dates = []
    for value in lastmods:
        match = ISO_DATE_RE.search(value)
        if match:
            try:
                date = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=timezone.utc)
            except ValueError:
                continue
            if date <= now:
                dates.append(date)
    return (max(dates) if dates else None), url


def _is_parked(text: str) -> str | None:
    lowered = text.casefold()
    for pattern in PARKED_PATTERNS:
        if pattern in lowered and len(lowered) < 3000:
            return pattern
    return None


def _resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, None)
        return True
    except OSError:
        return False


def check_liveness(url: str, client: httpx.Client | None = None, resolver=_resolves, now: datetime | None = None) -> dict:
    """{status, signals[{name, value, evidence, url}], checked_at, final_url}."""
    now = now or datetime.now(timezone.utc)
    signals: list[dict] = []
    result = {"status": "UNKNOWN", "signals": signals, "checked_at": now.isoformat(), "final_url": url}

    def done(status: str) -> dict:
        result["status"] = status
        return result

    host = host_of(url)
    if not host:
        signals.append(_signal("url", "invalid", url))
        return done("DEAD")
    if not resolver(host):
        signals.append(_signal("dns", "no address", host, url))
        return done("DEAD")
    signals.append(_signal("dns", "resolves", host, url))

    own_client = client is None
    client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers=BROWSER_HEADERS)
    try:
        try:
            resp = client.get(url)
            signals.append(_signal("tls", "valid" if url.startswith("https") else "not https", "", url))
        except httpx.ConnectError as exc:
            if "CERTIFICATE" not in str(exc).upper() and "SSL" not in str(exc).upper():
                signals.append(_signal("connection", "failed", exc, url))
                return done("DEAD")
            # Broken certificates are common on small, alive sites: note it
            # and read the page anyway (nothing from it reaches a prompt here).
            signals.append(_signal("tls", "invalid", exc, url))
            with httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers=BROWSER_HEADERS, verify=False) as insecure:
                resp = insecure.get(url)
        except httpx.HTTPError as exc:
            signals.append(_signal("connection", "failed", exc, url))
            return done("DEAD")

        final_url = str(resp.url)
        result["final_url"] = final_url
        signals.append(_signal("http_status", resp.status_code, "", final_url))
        if resp.status_code >= 400:
            return done("DEAD")
        if registrable_domain(final_url) != registrable_domain(url):
            signals.append(_signal("redirected_to_other_domain", registrable_domain(final_url), final_url, final_url))

        html = resp.text
        text = _extract_main_text(html, url=final_url)
        if len(text) < MIN_TEXT_CHARS:
            for candidate in _js_redirect_candidates(html, final_url):
                try:
                    alt = client.get(candidate)
                except httpx.HTTPError:
                    continue
                alt_text = _extract_main_text(alt.text, url=candidate)
                if alt.status_code < 400 and len(alt_text) > len(text):
                    signals.append(_signal("js_redirect", candidate, "home page is a JavaScript redirect shell", final_url))
                    html, text, final_url = alt.text, alt_text, str(alt.url)
                    break
        signals.append(_signal("text_chars", len(text), "", final_url))
        # kept for the code-side sector filter (never sent to a model from here)
        title = BeautifulSoup(html, "html.parser").title
        result["text"] = ((title.get_text(" ", strip=True) + "\n") if title else "") + text[:4000]

        parked = _is_parked(BeautifulSoup(html, "html.parser").get_text(" ") if len(text) < MIN_TEXT_CHARS * 5 else text)
        if parked:
            signals.append(_signal("parked", True, parked, final_url))
            return done("DEAD")

        signals.append(_signal("careers_page", bool(_find_careers_link(html, final_url)), "", final_url))

        freshness: list[datetime] = []
        year = _copyright_year(html, now)
        if year:
            signals.append(_signal("copyright_year", year, f"© {year}", final_url))
            freshness.append(datetime(year, 12, 31, tzinfo=timezone.utc))
        lastmod, sitemap_url = _sitemap_lastmod(client, final_url, now)
        if lastmod:
            signals.append(_signal("sitemap_lastmod", lastmod.date().isoformat(), "", sitemap_url))
            freshness.append(lastmod)
        header = resp.headers.get("last-modified")
        if header:
            try:
                modified = parsedate_to_datetime(header)
                signals.append(_signal("last_modified_header", modified.date().isoformat(), header, final_url))
                freshness.append(modified)
            except (TypeError, ValueError):
                pass
        news_url = _find_link(html, final_url, NEWS_KEYWORDS, [])
        if news_url and urlparse(news_url).scheme in ("http", "https"):
            try:
                news = client.get(news_url)
                dates = _dates_in(BeautifulSoup(news.text, "html.parser").get_text(" "), now) if news.status_code < 400 else []
                if dates:
                    newest = max(dates)
                    signals.append(_signal("news_newest_date", newest.date().isoformat(), "", news_url))
                    freshness.append(newest)
            except httpx.HTTPError:
                pass
    finally:
        if own_client:
            client.close()

    if not freshness:
        return done("UNKNOWN")
    newest = max(freshness)
    signals.append(_signal("newest_activity", newest.date().isoformat()))
    stale_after = get_settings().liveness_stale_years
    return done("STALE" if (now - newest).days > stale_after * 365 else "ALIVE")


def check_many(urls: list[str], concurrency: int | None = None, check=None) -> dict[str, dict]:
    """Liveness for many URLs: at most `concurrency` hosts at once, and one
    request at a time per host (same-host URLs run in one worker, in order)."""
    from concurrent.futures import ThreadPoolExecutor

    check = check or check_liveness
    by_host: dict[str, list[str]] = {}
    for url in dict.fromkeys(urls):
        by_host.setdefault(host_of(url), []).append(url)

    def run_host(host_urls: list[str]) -> list[tuple[str, dict]]:
        out = []
        for url in host_urls:
            try:
                out.append((url, check(url)))
            except Exception as exc:  # noqa: BLE001 - one bad site never stops the batch
                out.append((url, {"status": "UNKNOWN", "signals": [_signal("error", type(exc).__name__, exc, url)],
                                  "checked_at": datetime.now(timezone.utc).isoformat()}))
        return out

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=concurrency or get_settings().liveness_concurrency) as pool:
        for pairs in pool.map(run_host, by_host.values()):
            results.update(pairs)
    return results
