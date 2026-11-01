"""Cross-site request guard for the local review UI (README "Security
boundaries"). The app has no login, so the owner's own browser is the attack
surface: any page open in it could submit a hidden form to 127.0.0.1 and, e.g.,
rewrite a pending draft and approve it. Two checks close that:

- Host allowlist on every request - a DNS-rebinding page reaches us under its
  own hostname, so anything not addressed to localhost is refused.
- Same-origin check on every state-changing request - `Sec-Fetch-Site` when the
  browser sends it, otherwise `Origin`, then `Referer`, must point back at this
  app. A request that proves neither is refused rather than trusted.
"""
from urllib.parse import urlsplit

from starlette.requests import Request

from app.config import get_settings

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# "none" is a user-initiated navigation (typed URL, bookmark) - never a page.
ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})


def _hostname(host_header: str) -> str:
    """`127.0.0.1:8000` -> `127.0.0.1`, `[::1]:8000` -> `::1`."""
    host = host_header.strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def allowed_hostnames() -> set[str]:
    return {h.strip().lower() for h in get_settings().allowed_hosts.split(",") if h.strip()}


def host_is_allowed(request: Request) -> bool:
    return _hostname(request.headers.get("host", "")) in allowed_hostnames()


def _same_origin(url: str, request: Request) -> bool:
    parts = urlsplit(url)
    own_host = request.headers.get("host", "").lower()
    return bool(parts.netloc) and parts.scheme == request.url.scheme and parts.netloc.lower() == own_host


def cross_site_reason(request: Request) -> str | None:
    """Why this request must be refused, or None when it is fine. Safe methods
    are never blocked here - they don't change state."""
    if request.method not in UNSAFE_METHODS:
        return None

    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        if fetch_site.lower() in ALLOWED_FETCH_SITES:
            return None
        return f"Sec-Fetch-Site is {fetch_site!r}"

    origin = request.headers.get("origin")
    if origin is not None:
        return None if _same_origin(origin, request) else f"Origin {origin!r} is not this app"

    referer = request.headers.get("referer")
    if referer is not None:
        return None if _same_origin(referer, request) else "Referer is not this app"

    return "no Sec-Fetch-Site, Origin or Referer header to prove the request came from this app"


def same_host_path(url: str | None, request: Request, default: str = "/") -> str:
    """The path+query of `url` when it points back at this app, else `default` -
    keeps redirects built from request headers from leaving the app."""
    if not url:
        return default
    parts = urlsplit(url)
    if parts.netloc and parts.netloc.lower() != request.headers.get("host", "").lower():
        return default
    path = parts.path or "/"
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return default
    return path + (f"?{parts.query}" if parts.query else "")
