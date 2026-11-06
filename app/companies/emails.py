"""Deterministic email extraction from a page's raw HTML (CLAUDE.md section 8:
code finds the emails, the model only summarizes).

Readability's main-article text drops footers, `get_text` drops `mailto:`
hrefs, and Cloudflare shows protected addresses as "[email protected]" - so
an LLM reading extracted text never sees many real contact addresses. This
module reads them from the HTML itself:
- `mailto:` links (anchor text as context),
- Cloudflare `data-cfemail` spans and `/cdn-cgi/l/email-protection#...` links,
- plain-text addresses, including the common "[at]" / "(at)" / " at ... dot "
  obfuscations (conservative: the obfuscated form must say both at and dot).
"""
import re
from urllib.parse import unquote

from bs4 import BeautifulSoup

from app.email_send.gmail_client import validate_email

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# "name [at] acme [dot] com", "name(at)acme.com", "name at acme dot com".
_AT = r"\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|\s+at\s+)\s*"
_DOT = r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|\s+dot\s+|\.)\s*"
OBFUSCATED_RE = re.compile(
    rf"\b([A-Za-z0-9._%+-]+){_AT}([A-Za-z0-9-]+(?:{_DOT}[A-Za-z0-9-]+)*{_DOT}[A-Za-z]{{2,}})\b",
    re.IGNORECASE,
)
_DOT_SPLIT_RE = re.compile(_DOT, re.IGNORECASE)
# "logo@2x.png" and friends look like addresses to the regex.
_NOT_A_TLD = {"png", "jpg", "jpeg", "gif", "svg", "webp", "js", "css", "ico", "avif"}
CONTEXT_CHARS = 60
CF_PROTECTION_PATH = "/cdn-cgi/l/email-protection#"


def decode_cfemail(hex_text: str) -> str:
    """Cloudflare's email obfuscation: first byte is the XOR key."""
    try:
        key = int(hex_text[:2], 16)
        return "".join(chr(int(hex_text[i : i + 2], 16) ^ key) for i in range(2, len(hex_text), 2))
    except ValueError:
        return ""


def _clean(address: str) -> str:
    return unquote(address).strip().strip(".,;:()[]<>\"'").lower()


def _plausible(address: str) -> bool:
    if not validate_email(address):
        return False
    return address.rsplit(".", 1)[-1].lower() not in _NOT_A_TLD


def _context(text: str, start: int, end: int) -> str:
    snippet = text[max(0, start - CONTEXT_CHARS) : end + CONTEXT_CHARS]
    return re.sub(r"\s+", " ", snippet).strip()


def extract_emails(html: str, url: str) -> list[dict]:
    """Every address on the page, as [{email, context, url}], deduped
    case-insensitively in page order."""
    soup = BeautifulSoup(html or "", "html.parser")
    found: list[dict] = []
    seen: set[str] = set()

    def add(address: str, context: str) -> None:
        address = _clean(address)
        if address and address not in seen and _plausible(address):
            seen.add(address)
            found.append({"email": address, "context": context.strip()[:200], "url": url})

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        label = a.get_text(" ", strip=True)
        if href.lower().startswith("mailto:"):
            add(href[7:].split("?")[0], label)
        elif CF_PROTECTION_PATH in href:
            add(decode_cfemail(href.split("#", 1)[1]), label)
    for element in soup.select("[data-cfemail]"):
        parent = element.find_parent(["p", "li", "div", "td", "address"]) or element
        add(decode_cfemail(element["data-cfemail"]), parent.get_text(" ", strip=True))

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ")
    for match in EMAIL_RE.finditer(text):
        add(match.group(0), _context(text, match.start(), match.end()))
    for match in OBFUSCATED_RE.finditer(text):
        domain = ".".join(part for part in _DOT_SPLIT_RE.split(match.group(2)) if part)
        add(f"{match.group(1)}@{domain}", _context(text, match.start(), match.end()))
    return found


def merge_emails(deterministic: list[dict], from_llm: list[dict]) -> list[dict]:
    """Deterministic addresses first (they carry the page URL), then any the
    model reported that code didn't find; deduped case-insensitively."""
    merged: list[dict] = []
    seen: set[str] = set()
    for source, entries in (("page", deterministic), ("llm", from_llm)):
        for entry in entries:
            address = (entry.get("email") or "").strip()
            key = address.lower()
            if not address or key in seen:
                continue
            seen.add(key)
            merged.append({**entry, "email": address, "found_by": source})
    return merged
