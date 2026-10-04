"""
Bulk company-list parsing for the companies page.

The owner pastes one company per line, in whatever loose format their list is
in - the parser detects the pieces instead of enforcing a single delimiter:

    BeeVision= https://www.beevision.com.tr
    3Y Teknoloji | 3yteknoloji.com.tr
    adesso, https://www.adesso.com.tr, jobs@adesso.com.tr
    Jotform<TAB>https://www.jotform.com

Rules (mirrors the single-add form, but website is required here because the
whole point of bulk add is running research on each row):
- an email address anywhere on the line becomes contact_email
- the first explicit URL (http(s):// or www.) becomes website_url; if none,
  the text after the first delimiter is checked for a bare domain
- everything before the URL/delimiter, minus trailing delimiter chars, is the name
- rows without a usable name AND website are returned with an `error` so the
  owner sees them flagged instead of having them silently dropped
"""
import re
from urllib.parse import urlparse

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
EXPLICIT_URL_RE = re.compile(r"(?:https?://|www\.)[^\s|,;]+", re.IGNORECASE)
BARE_DOMAIN_RE = re.compile(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/[^\s|,;]*)?", re.IGNORECASE)
DELIMITERS = ("=", "|", ";", "\t", ",")
_TRIM_CHARS = " \t=|,;:>-"


def normalize_url(raw: str) -> str:
    url = raw.strip().rstrip(".,;")
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return url


def domain_of(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    return netloc.removeprefix("www.")


def _split_on_first_delimiter(line: str) -> tuple[str, str]:
    positions = [(line.find(d), d) for d in DELIMITERS if line.find(d) != -1]
    if not positions:
        return line, ""
    pos, delim = min(positions)
    return line[:pos], line[pos + len(delim):]


def parse_line(raw: str) -> dict:
    """Parse one pasted line into {name, website_url, contact_email, error}."""
    row = {"raw": raw.strip(), "name": "", "website_url": "", "contact_email": "", "error": ""}
    line = raw.strip()

    email_match = EMAIL_RE.search(line)
    if email_match:
        row["contact_email"] = email_match.group(0)
        line = line.replace(email_match.group(0), " ")

    url_match = EXPLICIT_URL_RE.search(line)
    if url_match:
        row["website_url"] = normalize_url(url_match.group(0))
        row["name"] = line[: url_match.start()].strip(_TRIM_CHARS)
    else:
        # No http(s)/www URL - only look for a bare domain AFTER a delimiter,
        # so a dotted company name ("invent.ai") is never eaten as the URL.
        name_part, rest = _split_on_first_delimiter(line)
        row["name"] = name_part.strip(_TRIM_CHARS)
        bare = BARE_DOMAIN_RE.search(rest)
        if bare:
            row["website_url"] = normalize_url(bare.group(0))
        elif not row["name"]:
            row["name"] = line.strip(_TRIM_CHARS)

    if not row["name"]:
        row["error"] = "missing_name"
    elif not row["website_url"]:
        row["error"] = "missing_website"
    return row


def parse_bulk_text(text: str) -> list[dict]:
    """Parse the pasted blob; one row per non-empty line, in-batch duplicates
    (same name or same domain) flagged so the same company is never added twice."""
    rows = []
    seen_names: set[str] = set()
    seen_domains: set[str] = set()
    for raw in text.splitlines():
        if not raw.strip():
            continue
        row = parse_line(raw)
        if not row["error"]:
            name_key = row["name"].casefold()
            domain = domain_of(row["website_url"])
            if name_key in seen_names or (domain and domain in seen_domains):
                row["error"] = "duplicate_in_batch"
            else:
                seen_names.add(name_key)
                if domain:
                    seen_domains.add(domain)
        rows.append(row)
    return rows
