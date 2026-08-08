"""Thin wrapper around Jinja2Templates.TemplateResponse that injects the current
language + translator into every render, so routes don't repeat that boilerplate."""
from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.web.i18n import get_lang, translator


def render(templates: Jinja2Templates, request: Request, name: str, context: dict | None = None):
    context = dict(context or {})
    lang = get_lang(request)
    context["lang"] = lang
    context["t"] = translator(lang)
    return templates.TemplateResponse(request, name, context)
