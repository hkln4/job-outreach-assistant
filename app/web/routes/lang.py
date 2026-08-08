"""Language switcher for the review UI chrome (CLAUDE.md section 1 keeps
generated/stored content English-only - this is UI labels only)."""
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.web.i18n import SUPPORTED_LANGS

router = APIRouter(tags=["lang"])


@router.get("/set-lang/{lang}")
def set_lang(lang: str, request: Request):
    target = lang if lang in SUPPORTED_LANGS else "en"
    next_url = request.headers.get("referer", "/")
    response = RedirectResponse(url=next_url, status_code=303)
    response.set_cookie("lang", target, max_age=60 * 60 * 24 * 365)
    return response
