"""Thin wrapper around Jinja2Templates.TemplateResponse that injects the current
language + translator into every render, so routes don't repeat that boilerplate."""
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.web.i18n import get_lang, translator


STATIC_DIR = Path(__file__).parent / "static"


def static_url(name: str) -> str:
    """/static/<name>?v=<mtime>, so a browser never runs a stale script or
    stylesheet after the app is updated."""
    try:
        version = int((STATIC_DIR / name).stat().st_mtime)
    except OSError:
        version = 0
    return f"/static/{name}?v={version}"


def render(templates: Jinja2Templates, request: Request, name: str, context: dict | None = None):
    context = dict(context or {})
    lang = get_lang(request)
    context["lang"] = lang
    context["t"] = translator(lang)
    context["static"] = static_url
    return templates.TemplateResponse(request, name, context)
