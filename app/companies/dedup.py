"""Dedup guard (CLAUDE.md section 9) shared by every path that adds companies:
the single/bulk add form and the technopark directory discovery.

Directories list legal names ("Arpanet Bilişim Teknolojileri A.Ş.") and often
a different domain than the one the owner saved (beevision.ai vs
beevision.com.tr), so exact matching misses companies the owner already
tracks. Matching therefore also compares the domain's own label and the
name word by word - but whole words only: "Güven Future" must not match
"... Siber Güvenlik ..."."""
import re
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.companies.bulk_parse import domain_of
from app.models import Company

# Multi-part public suffixes seen in Turkish company sites; everything else
# is treated as a single-label TLD (.com, .ai, .io, .tr, ...).
_MULTI_PART_SUFFIXES = (
    "com.tr", "net.tr", "org.tr", "gen.tr", "web.tr", "biz.tr", "info.tr", "edu.tr",
    "gov.tr", "av.tr", "bel.tr", "k12.tr", "tsk.tr", "co.uk", "org.uk", "com.au", "com.br",
)
# Shorter labels/names collide too easily ("ai", "app", "tek").
MIN_MATCH_CHARS = 4
_TURKISH_FOLD = str.maketrans("çğıöşüâîû", "cgiosuaiu")


def normalize_url(url: str | None) -> str | None:
    """Directory listings often omit the scheme ("www.acme.com.tr")."""
    url = (url or "").strip().replace(" ", "").replace("%20", "")
    if not url:
        return None
    return url if url.startswith(("http://", "https://")) else f"https://{url}"


def site_label(url: str | None) -> str:
    """The domain's own name: beevision.com.tr and beevision.ai -> "beevision"."""
    if not url:
        return ""
    host = urlparse(normalize_url(url) or "").netloc.lower().split(":")[0].removeprefix("www.")
    for suffix in _MULTI_PART_SUFFIXES:
        if host.endswith("." + suffix):
            host = host[: -len(suffix) - 1]
            break
    else:
        host = host.rsplit(".", 1)[0] if "." in host else host
    return host.rsplit(".", 1)[-1]


def name_tokens(name: str) -> list[str]:
    folded = name.replace("İ", "i").replace("I", "i").lower().replace("̇", "").translate(_TURKISH_FOLD)
    return re.findall(r"[a-z0-9]+", folded)


def _names_match(tracked: list[str], listed: list[str]) -> bool:
    """The tracked name's words open the listed (legal) name: "Insider One" ~
    "Insider One (SOSYO PLUS ...)", "Fonet Bilgi Tek" ~ "FONET BİLGİ TEKNOLOJİLERİ".
    A single-word name must match a whole word ("Plan" never matches "PLANET")."""
    if len("".join(tracked)) < MIN_MATCH_CHARS or len(listed) < len(tracked):
        return False
    if len(tracked) == 1:
        return listed[0] == tracked[0]
    *head, last = tracked
    return listed[: len(head)] == head and listed[len(head)].startswith(last)


class CompanyIndex:
    """Built once per request/import so matching hundreds of candidates doesn't
    re-query every company each time."""

    def __init__(self, db: Session):
        self._entries: list[tuple[Company, str, str, list[list[str]]]] = []
        for company in db.query(Company).all():
            self.add(company)

    def add(self, company: Company) -> None:
        # "Ratio Energy / Ratio Technologies" -> either name counts.
        names = [name_tokens(part) for part in company.name.split("/") if part.strip()]
        url = company.website_url
        self._entries.append((company, domain_of(normalize_url(url)) if url else "", site_label(url), names))

    def find(
        self, name: str, website_url: str | None = None, brand_name: str | None = None, *, loose: bool = True
    ) -> Company | None:
        """loose=False is the exact check (same name, same domain) - for a company
        the owner types in by hand, where "Acme Robotics" next to "Acme" is a
        deliberate second entry. loose=True also matches directory legal names
        and sister domains."""
        domain = domain_of(normalize_url(website_url)) if website_url else ""
        label = site_label(website_url)
        listed = [tokens for tokens in (name_tokens(name), name_tokens(brand_name or "")) if tokens]
        for company, c_domain, c_label, c_names in self._entries:
            if company.name.casefold() == name.casefold():
                return company
            if domain and domain == c_domain:
                return company
            if not loose:
                continue
            if len(label) >= MIN_MATCH_CHARS and label == c_label:
                return company
            if any(_names_match(tracked, tokens) for tracked in c_names for tokens in listed):
                return company
        return None


def find_existing_company(db: Session, name: str, website_url: str | None) -> Company | None:
    """The add-company form's exact duplicate check."""
    return CompanyIndex(db).find(name, website_url, loose=False)
