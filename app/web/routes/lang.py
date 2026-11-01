"""Language switcher for the review UI chrome - labels only. Stored records stay
English and letters follow their own language rule (CLAUDE.md section 1)."""
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.web.i18n import SUPPORTED_LANGS
from app.web.security import same_host_path

router = APIRouter(tags=["lang"])


@router.get("/set-lang/{lang}")
def set_lang(lang: str, request: Request):
    target = lang if lang in SUPPORTED_LANGS else "en"
    # Back to the page the switch was clicked on - never to another site.
    next_url = same_host_path(request.headers.get("referer"), request)
    response = RedirectResponse(url=next_url, status_code=303)
    response.set_cookie("lang", target, max_age=60 * 60 * 24 * 365)
    return response
