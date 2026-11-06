"""Deterministic channel detection from a page's raw HTML.

The LLM never saw form markup (extracted text drops it), so its
"application_form" answer was a guess. This reads the HTML itself:
- <form>s that take a CV (a file input, or fields named/labelled cv, resume,
  özgeçmiş, başvuru), and contact forms (email + message, no CV);
- links, iframes, scripts and form actions pointing at a known ATS host,
  matched by registrable domain (app/channels/domains.py), with the board
  token parsed where the provider's URL shape carries one;
- account-only portals (kariyer.net, LinkedIn jobs), reported as links only.

Each finding carries the snippet that triggered it and the page it is on. The
same module extracts a form's schema (fields, required, maxlength, options,
file inputs, CAPTCHA) for the review page - nothing here ever fills or submits
a form.
"""
import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from app.channels.domains import host_matches, host_of

# provider, domain, optional path prefix the URL must start with, kind
ATS_HOSTS: tuple[tuple[str, str, str, str], ...] = (
    ("kolayik", "kolayik.com", "", "ats_board"),
    ("workable", "workable.com", "", "ats_board"),
    ("lever", "lever.co", "", "ats_board"),
    ("greenhouse", "greenhouse.io", "", "ats_board"),
    ("ashby", "ashbyhq.com", "", "ats_board"),
    ("smartrecruiters", "smartrecruiters.com", "", "ats_board"),
    ("teamtailor", "teamtailor.com", "", "ats_board"),
    ("personio", "personio.de", "", "ats_board"),
    ("personio", "personio.com", "", "ats_board"),
    ("bamboohr", "bamboohr.com", "", "ats_board"),
    ("recruitee", "recruitee.com", "", "ats_board"),
    ("breezy", "breezy.hr", "", "ats_board"),
    ("jotform", "jotform.com", "", "site_form"),
    ("google_forms", "docs.google.com", "/forms", "site_form"),
    ("google_forms", "forms.gle", "", "site_form"),
    ("typeform", "typeform.com", "", "site_form"),
    ("hubspot_forms", "hsforms.com", "", "site_form"),
    ("hubspot_forms", "hsforms.net", "", "site_form"),
    ("kariyer.net", "kariyer.net", "", "portal"),
    ("linkedin_jobs", "linkedin.com", "/jobs", "portal"),
)
# Marketing/legal pages on an ATS vendor's own site are not a company's board.
_VENDOR_ROOT_PATHS = ("", "/", "/pricing", "/blog", "/about", "/privacy", "/terms")

CV_FIELD_RE = re.compile(r"\bcv\b|resume|résumé|özgeçmiş|ozgecmis|başvuru|basvuru|curriculum|cover.?letter|ön yazı", re.IGNORECASE)
MESSAGE_FIELD_RE = re.compile(r"message|mesaj|comment|yorum|konu|subject|ileti", re.IGNORECASE)
SEARCH_FIELD_NAMES = {"q", "s", "search", "query", "ara", "keyword"}
CAPTCHA_RE = re.compile(r"g-recaptcha|recaptcha|h-captcha|hcaptcha|cf-turnstile|turnstile", re.IGNORECASE)
SNIPPET_CHARS = 200


def _snippet(element: Tag) -> str:
    text = element.get_text(" ", strip=True)
    if not text:
        attrs = {k: v for k, v in element.attrs.items() if k in ("href", "src", "action", "name", "id", "title")}
        text = f"<{element.name} {attrs}>"
    return re.sub(r"\s+", " ", text)[:SNIPPET_CHARS]


def board_token(provider: str, url: str) -> str:
    """The company slug inside an ATS URL, or "" when the shape has none."""
    parsed = urlparse(url)
    host = host_of(url)
    parts = [p for p in parsed.path.split("/") if p]
    query = parse_qs(parsed.query)
    if provider == "greenhouse":
        if "for" in query:
            return query["for"][0]
        if parts and parts[0] not in ("embed", "v1"):
            return parts[0]
        return ""
    if provider in ("lever", "ashby", "smartrecruiters", "workable"):
        if host.split(".")[0] in ("jobs", "careers", "apply", "boards", "www", "api") and parts:
            return parts[0] if parts[0] not in ("v0", "v1", "posting-api", "companies") else (parts[-1] if len(parts) > 1 else "")
        return ""
    if provider in ("kolayik", "teamtailor", "recruitee", "breezy", "bamboohr", "personio"):
        sub = host.split(".")[0]
        return "" if sub in ("www", "app", "api", "jobs", "careers") else sub
    return ""


def classify_ats_url(url: str) -> dict | None:
    """{provider, kind, token} for a URL on a known ATS / form / portal host."""
    if not url.startswith(("http://", "https://", "//")):
        return None
    if url.startswith("//"):
        url = "https:" + url
    path = urlparse(url).path or "/"
    for provider, domain, prefix, kind in ATS_HOSTS:
        if not host_matches(url, domain):
            continue
        if prefix and not path.startswith(prefix):
            continue
        token = board_token(provider, url)
        if kind == "ats_board" and not token and host_of(url) == domain and path.rstrip("/") in _VENDOR_ROOT_PATHS:
            continue  # a link to the vendor's own homepage, e.g. "powered by Workable"
        return {"provider": provider, "kind": kind, "token": token}
    return None


def _label_for(form: Tag, field: Tag) -> str:
    field_id = field.get("id")
    if field_id:
        label = form.find("label", attrs={"for": field_id})
        if label:
            return label.get_text(" ", strip=True)
    parent_label = field.find_parent("label")
    if parent_label:
        return parent_label.get_text(" ", strip=True)
    return field.get("placeholder") or field.get("aria-label") or ""


def form_schema(form: Tag, page_url: str) -> dict:
    """Fields of one <form>: label, name, type, required, maxlength, options."""
    fields = []
    for field in form.find_all(["input", "textarea", "select"]):
        field_type = (field.get("type") or field.name).lower()
        if field_type in ("hidden", "submit", "button", "reset", "image"):
            continue
        maxlength = field.get("maxlength")
        options = [o.get_text(" ", strip=True) for o in field.find_all("option")] if field.name == "select" else []
        if field_type in ("radio", "checkbox"):
            options = [field.get("value") or ""]
        fields.append(
            {
                "label": _label_for(form, field)[:120],
                "name": field.get("name") or "",
                "type": "textarea" if field.name == "textarea" else ("select" if field.name == "select" else field_type),
                "required": field.has_attr("required") or field.get("aria-required") == "true",
                "maxlength": int(maxlength) if str(maxlength or "").isdigit() else None,
                "options": [o for o in options if o][:30],
            }
        )
    markup = str(form)
    return {
        "action": urljoin(page_url, form.get("action") or page_url),
        "method": (form.get("method") or "get").lower(),
        "fields": fields,
        "has_file_input": any(f["type"] == "file" for f in fields),
        "has_captcha": bool(CAPTCHA_RE.search(markup)),
    }


def _classify_form(schema: dict) -> str | None:
    """"site_form" (takes a CV), "contact_form", or None (search, newsletter...)."""
    fields = schema["fields"]
    if not fields:
        return None
    names = [f"{f['name']} {f['label']}" for f in fields]
    if len(fields) <= 2 and any(f["name"].lower() in SEARCH_FIELD_NAMES or f["type"] == "search" for f in fields):
        return None
    if schema["has_file_input"] or any(CV_FIELD_RE.search(n) for n in names):
        return "site_form"
    has_email = any(f["type"] == "email" or "mail" in f["name"].lower() for f in fields)
    has_message = any(f["type"] == "textarea" or MESSAGE_FIELD_RE.search(n) for f, n in zip(fields, names))
    if has_email and has_message:
        return "contact_form"
    return None


def detect_channels(html: str, url: str) -> list[dict]:
    """Every channel on the page: [{kind, provider, url, token, page_url,
    evidence_snippet, form_schema?}], deduped by (kind, url)."""
    soup = BeautifulSoup(html or "", "html.parser")
    found: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(entry: dict) -> None:
        key = (entry["kind"], entry["url"])
        if key not in seen:
            seen.add(key)
            found.append({**entry, "page_url": url})

    for form in soup.find_all("form"):
        schema = form_schema(form, url)
        ats = classify_ats_url(schema["action"])
        kind = _classify_form(schema)
        if ats is not None:
            add({"kind": ats["kind"], "provider": ats["provider"], "token": ats["token"], "url": schema["action"],
                 "evidence_snippet": _snippet(form), "form_schema": schema})
        elif kind is not None:
            # The page itself is the form's address - the action is often a
            # script endpoint the owner never needs to open.
            add({"kind": kind, "provider": "", "token": "", "url": url, "evidence_snippet": _snippet(form),
                 "form_schema": schema})

    for element, attr in ((e, "href") for e in soup.find_all("a", href=True)):
        target = urljoin(url, element[attr].strip())
        ats = classify_ats_url(target)
        if ats is not None:
            add({"kind": ats["kind"], "provider": ats["provider"], "token": ats["token"], "url": target,
                 "evidence_snippet": _snippet(element)})
    for element in soup.find_all(["iframe", "script"], src=True):
        target = urljoin(url, element["src"].strip())
        ats = classify_ats_url(target)
        if ats is not None:
            add({"kind": ats["kind"], "provider": ats["provider"], "token": ats["token"], "url": target,
                 "evidence_snippet": _snippet(element)})
    return found


def merge_channels(*lists: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for entries in lists:
        for entry in entries:
            key = (entry["kind"], entry["url"])
            if key not in seen:
                seen.add(key)
                merged.append(entry)
    return merged
