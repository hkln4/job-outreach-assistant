"""Global error handling: no unhandled exception should ever reach the owner as
a raw traceback. This registers a catch-all on the FastAPI app and classifies
LLM provider errors (rate limit / billing / auth) so the error page can say
something actionable instead of just "something broke"."""
import logging

from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates

from app.web.i18n import get_lang, translator

logger = logging.getLogger("app.errors")

templates = Jinja2Templates(directory="app/web/templates")


class NotFoundError(ValueError):
    """A page for a record that doesn't exist (deleted, mistyped id) - shown as
    a plain 404 instead of an "unexpected error"."""


def classify_llm_error(exc: Exception) -> str:
    """Best-effort classification shared by anthropic and google-genai errors -
    both expose `.status_code` on their API exception types."""
    status = getattr(exc, "status_code", None)
    message = str(exc).lower()

    if status == 429 or "rate limit" in message or "quota" in message:
        return "rate_limit"
    if status in (401, 403) or "invalid api key" in message or "authentication" in message:
        return "auth"
    if "credit" in message or "billing" in message or "balance is too low" in message:
        return "billing"
    return "unknown"


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception):
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        lang = get_lang(request)
        t = translator(lang)
        category = "not_found" if isinstance(exc, NotFoundError) else classify_llm_error(exc)
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "lang": lang,
                "t": t,
                "category": category,
                "detail": str(exc),
            },
            status_code=404 if category == "not_found" else 500,
        )
