"""Thin wrapper around Jinja2Templates.TemplateResponse that injects the current
language + translator into every render, so routes don't repeat that boilerplate."""
from pathlib import Path

from fastapi import Request
from markupsafe import Markup
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


def simple_markdown(text: str) -> Markup:
    """Headings (#, ##, ###), "- " lists and paragraphs - enough for the
    "about me" document. Everything is escaped first."""
    out, items, para = [], [], []

    def flush():
        if para:
            out.append(Markup("<p>{}</p>").format(" ".join(para)))
            para.clear()
        if items:
            out.append(Markup("<ul>") + Markup("").join(Markup("<li>{}</li>").format(i) for i in items) + Markup("</ul>"))
            items.clear()

    for line in (text or "").splitlines():
        stripped = line.strip()
        level = len(stripped) - len(stripped.lstrip("#"))
        if 1 <= level <= 3 and stripped[level:level + 1] == " ":
            flush()
            out.append(Markup("<h{0}>{1}</h{0}>").format(level + 2, stripped[level + 1:].strip()))
        elif stripped.startswith(("- ", "* ")):
            if para:
                flush()
            items.append(stripped[2:])
        elif not stripped:
            flush()
        else:
            if items:
                flush()
            para.append(stripped)
    flush()
    return Markup("").join(out)


def render(templates: Jinja2Templates, request: Request, name: str, context: dict | None = None):
    context = dict(context or {})
    lang = get_lang(request)
    context["lang"] = lang
    context["t"] = translator(lang)
    context["static"] = static_url
    context["markdown"] = simple_markdown
    return templates.TemplateResponse(request, name, context)
