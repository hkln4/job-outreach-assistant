"""Discovery sources beyond the built-in technopark readers (roadmap phase 2).
All owner-triggered, all feeding the same candidate -> approval path:

- "file": a CSV/XLSX list the owner downloaded himself (e.g. the Ministry of
  Industry and Technology's Ar-Ge merkezi list), with a column mapping;
- "json": a JS-only directory (e.g. Gazi Teknopark) read from the JSON
  endpoint the owner copied from the browser's network tab, with a field
  mapping - read like the ODTÜ reader, never with an LLM;
- "github_orgs": GitHub organizations by location (search API
  `type:org location:<city>`), with each org's newest repo push as an
  activity signal and its `blog` field as the website.
"""
import csv
import io
import re
from datetime import datetime

from app.applications.synonyms import fold
from app.discovery.sources import DirectoryReadError, _client, _entry

MAX_ROWS = 5000
MAX_GITHUB_ORGS = 100
MAPPABLE_FIELDS = ("name", "website", "email", "sector", "city")


def table_rows(filename: str, data: bytes) -> list[dict[str, str]]:
    """Rows of a CSV or XLSX file as {header: value} (first row = headers)."""
    if filename.lower().endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        sheet = load_workbook(io.BytesIO(data), read_only=True, data_only=True).active
        values = [[("" if cell is None else str(cell)).strip() for cell in row] for row in sheet.iter_rows(values_only=True)]
    else:
        text = data.decode("utf-8-sig", errors="replace")
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t") if text.strip() else csv.excel
        values = [[v.strip() for v in row] for row in csv.reader(io.StringIO(text), dialect)]
    values = [row for row in values if any(row)]
    if not values:
        return []
    headers = [h or f"column_{i + 1}" for i, h in enumerate(values[0])]
    return [dict(zip(headers, row + [""] * (len(headers) - len(row)))) for row in values[1 : MAX_ROWS + 1]]


def guess_mapping(headers: list[str]) -> dict[str, str]:
    """A first guess at which column is which; the owner confirms it."""
    hints = {
        "name": ("firma", "şirket", "sirket", "unvan", "ünvan", "company", "name", "ad"),
        "website": ("web", "site", "url", "internet"),
        "email": ("mail", "e-posta", "eposta"),
        "sector": ("sektör", "sektor", "sector", "faaliyet", "alan", "industry"),
        "city": ("il", "şehir", "sehir", "city", "location"),
    }
    def matches(header: str, word: str) -> bool:
        folded, word = fold(header), fold(word)
        # short hints ("il", "ad") must be a whole word, or "ilan" would be a city
        return word in re.findall(r"\w+", folded) if len(word) <= 3 else word in folded

    mapping = {}
    for field, words in hints.items():
        for header in headers:
            if any(matches(header, word) for word in words) and header not in mapping.values():
                mapping[field] = header
                break
    return mapping


def entries_from_rows(rows: list[dict], mapping: dict[str, str]) -> list[dict]:
    if not mapping.get("name"):
        raise DirectoryReadError("Pick which column holds the company name.")
    entries = []
    for row in rows:
        name = (row.get(mapping["name"]) or "").strip()
        if not name:
            continue
        entries.append(
            {
                **_entry(
                    name,
                    row.get(mapping.get("website", "")) or None,
                    contact_email=row.get(mapping.get("email", "")) or "",
                    sector=row.get(mapping.get("sector", "")) or "",
                ),
                "city": (row.get(mapping.get("city", "")) or "").strip(),
            }
        )
    return entries


def _walk(data, path: str):
    for key in [p for p in (path or "").split(".") if p]:
        if isinstance(data, dict):
            data = data.get(key)
        elif isinstance(data, list) and key.isdigit():
            data = data[int(key)] if int(key) < len(data) else None
        else:
            return None
    return data


def read_json_endpoint(url: str, config: dict) -> list[dict]:
    """Items at `items_path` of the JSON, fields picked by dotted paths in
    config (`name`, `website`, `email`, `sector`)."""
    with _client() as client:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()
    items = _walk(data, config.get("items_path", ""))
    if not isinstance(items, list):
        raise DirectoryReadError("The JSON has no list at that items path - check it against the network tab.")
    entries = []
    for item in items:
        name = _walk(item, config.get("name") or "name")
        if isinstance(name, str) and name.strip():
            entries.append(
                _entry(
                    name,
                    _walk(item, config.get("website", "")) if config.get("website") else None,
                    contact_email=str(_walk(item, config.get("email", "")) or "") if config.get("email") else "",
                    sector=str(_walk(item, config.get("sector", "")) or "") if config.get("sector") else "",
                )
            )
    return entries


def read_github_orgs(location: str, token: str, gh=None) -> list[dict]:
    """Organizations GitHub lists at `location`, newest-activity first."""
    from github import Github

    if not token and gh is None:
        raise DirectoryReadError("GITHUB_TOKEN is not set - the GitHub search API needs it.")
    gh = gh or Github(token)
    entries = []
    for org in gh.search_users(f"type:org location:{location}")[:MAX_GITHUB_ORGS]:
        newest = None
        try:
            repos = org.get_repos(sort="pushed", direction="desc")
            first = next(iter(repos), None)
            newest = first.pushed_at if first is not None else None
        except Exception:  # noqa: BLE001 - activity is a nice-to-have signal
            pass
        entry = _entry(org.name or org.login, getattr(org, "blog", None) or None, contact_email=getattr(org, "email", "") or "")
        entry["github"] = {
            "login": org.login,
            "url": org.html_url,
            "public_repos": getattr(org, "public_repos", 0),
            "newest_push": newest.isoformat() if isinstance(newest, datetime) else "",
        }
        entry["sector"] = f"GitHub org · {entry['github']['public_repos']} public repos"
        entries.append(entry)
    entries.sort(key=lambda e: e["github"]["newest_push"], reverse=True)
    return entries
