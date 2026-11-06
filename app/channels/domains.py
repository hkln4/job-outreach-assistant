"""Registrable-domain matching without a network fetch of the public suffix
list. Only the multi-label suffixes this app meets in practice are listed
(Turkey first); anything else falls back to the last two labels, which is
right for every single-label TLD."""
from urllib.parse import urlparse

MULTI_LABEL_SUFFIXES = {
    "com.tr", "net.tr", "org.tr", "gov.tr", "edu.tr", "gen.tr", "biz.tr", "info.tr",
    "av.tr", "k12.tr", "web.tr", "bel.tr", "pol.tr", "tsk.tr", "name.tr", "tel.tr",
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk",
    "com.au", "net.au", "org.au", "co.nz", "co.jp", "co.kr", "com.br", "com.cn",
    "co.in", "co.za", "com.mx", "com.sg", "com.de", "co.il", "com.cy",
}


def host_of(url_or_host: str) -> str:
    value = (url_or_host or "").strip().lower()
    if "//" in value:
        value = urlparse(value).hostname or ""
    else:
        value = value.split("/", 1)[0].split(":", 1)[0]
    return value.rstrip(".").removeprefix("www.")


def registrable_domain(url_or_host: str) -> str:
    """"jobs.acme.com.tr" -> "acme.com.tr", "boards.greenhouse.io" -> "greenhouse.io"."""
    labels = [label for label in host_of(url_or_host).split(".") if label]
    if len(labels) <= 2:
        return ".".join(labels)
    if ".".join(labels[-2:]) in MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def host_matches(url_or_host: str, domain: str) -> bool:
    """True when the host IS `domain` or a subdomain of it - so
    "evil-greenhouse.io" and "greenhouse.io.example.com" never match."""
    host = host_of(url_or_host)
    domain = domain.lower().strip(".")
    return host == domain or host.endswith("." + domain)
