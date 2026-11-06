"""Rendering a generated CV (roadmap phase 5): a deterministic Typst template
(app/cv_builder/templates/cv.typ) in the owner's FlowCV layout, Lato bundled,
compiled in-process by the `typst` package - no system install.

Page limit (CV_PAGE_LIMIT, default 1): if the CV runs over, the font steps
down a little, then the last project's lowest-ranked bullets are dropped, one
at a time - never the locked Education / Experience text.
"""
import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.applications.signature import owner_identity
from app.config import get_settings
from app.cv_builder.assemble import locked_sections
from app.cv_builder.icons import ICONS
from app.cv_builder.skills import GROUP_TITLES, GROUPS
from app.models import Bullet, CVProject, GeneratedCV

TEMPLATE_DIR = Path(__file__).parent / "templates"
FONT_DIR = Path(__file__).parent / "fonts"
FONT_SIZES = (9.5, 9.2, 9.0, 8.8, 8.6)
HEADINGS = {
    "en": {"education": "Education", "experience": "Professional Experience", "skills": "Technical Skills", "projects": "Projects", "certificates": "Certificates"},
    "tr": {"education": "Eğitim", "experience": "Profesyonel Deneyim", "skills": "Teknik Beceriler", "projects": "Projeler", "certificates": "Sertifikalar"},
}


class RenderUnavailable(RuntimeError):
    """The typst package isn't installed."""


def _dates(entry: dict) -> str:
    return " – ".join(part for part in (entry.get("start", ""), entry.get("end", "")) if part)


def _contact_text(field: str, value: str) -> str:
    return value.removeprefix("https://").removeprefix("http://").removeprefix("www.").rstrip("/") if field.endswith("_url") else value


def build_data(db: Session, generated: GeneratedCV, max_bullets: dict[int, int] | None = None, font_size: float = FONT_SIZES[0]) -> dict:
    """The template's input, from the selection + bank + locked sections."""
    selection = generated.selection or {}
    language = generated.language if generated.language in HEADINGS else "en"
    headings = HEADINGS[language]
    identity = owner_identity(db)
    locked = locked_sections(db)

    contacts = []
    for field, icon in (("email", "email"), ("linkedin_url", "linkedin"), ("github_url", "github"), ("portfolio_url", "portfolio"), ("phone", "phone"), ("city", "city")):
        if identity.get(field):
            contacts.append({"icon": ICONS[icon], "text": _contact_text(field, identity[field])})

    sections = []
    if locked["education"]:
        sections.append({"kind": "education", "title": headings["education"],
                         "entries": [{**e, "dates": _dates(e)} for e in locked["education"]]})
    if locked["experience"]:
        sections.append({"kind": "experience", "title": headings["experience"],
                         "entries": [{**e, "dates": _dates(e)} for e in locked["experience"]]})
    groups = [{"title": GROUP_TITLES[language][g], "items": selection.get("skills", {}).get(g, [])} for g in GROUPS]
    groups = [g for g in groups if g["items"]]
    if groups:
        sections.append({"kind": "skills", "title": headings["skills"], "groups": groups})

    overrides = selection.get("bullet_overrides") or {}
    projects = []
    for project_id in selection.get("project_ids") or []:
        project = db.get(CVProject, project_id)
        if project is None:
            continue
        ids = (selection.get("bullet_ids") or {}).get(str(project_id), [])
        if max_bullets and project_id in max_bullets:
            ids = ids[: max_bullets[project_id]]
        texts = []
        for bullet_id in ids:
            bullet = db.get(Bullet, bullet_id)
            if bullet is not None and bullet.status == "approved":
                texts.append(overrides.get(str(bullet_id)) or bullet.text)
        title = project.name + (f" — {project.subtitle}" if project.subtitle else "")
        projects.append({"title": title, "link": project.link_url or "", "bullets": texts})
    if projects:
        sections.append({"kind": "projects", "title": headings["projects"], "projects": projects})
    if selection.get("certificates") and locked["certificates"]:
        sections.append({"kind": "certificates", "title": headings["certificates"],
                         "items": [" | ".join(p for p in (c["title"], c["organization"]) if p) for c in locked["certificates"]]})
    return {
        "lang": language,
        "font_size": font_size,
        "name": identity.get("full_name", ""),
        "title": identity.get("title", ""),
        "contacts": contacts,
        "link_icon": ICONS["link"],
        "sections": sections,
    }


def compile_pdf(data: dict) -> bytes:
    try:
        import typst
    except ImportError as exc:  # pragma: no cover - listed in requirements.txt
        raise RenderUnavailable("The typst package isn't installed - run pip install -r requirements.txt.") from exc
    return typst.compile(
        (TEMPLATE_DIR / "cv.typ").read_bytes(),
        format="pdf",
        root=str(TEMPLATE_DIR),
        font_paths=[str(FONT_DIR)],
        sys_inputs={"data": json.dumps(data, ensure_ascii=False)},
    )


def page_count(pdf: bytes) -> int:
    import io

    from pypdf import PdfReader

    return len(PdfReader(io.BytesIO(pdf)).pages)


def source_strings(data: dict) -> list[str]:
    """Every string the template printed - for the ATS word-diff check."""
    out = [data["name"], data["title"]] + [c["text"] for c in data["contacts"]]
    for section in data["sections"]:
        out.append(section["title"])
        for entry in section.get("entries", []):
            out += [entry["title"], entry["organization"], entry["dates"], entry["location"], *entry["bullets"]]
        for group in section.get("groups", []):
            out += [group["title"], ", ".join(group["items"])]
        for project in section.get("projects", []):
            out += [project["title"], *project["bullets"]]
        out += section.get("items", [])
    return [s for s in out if s]


def render(db: Session, generated: GeneratedCV) -> tuple[bytes, dict]:
    """(pdf, data) within the page limit."""
    limit = get_settings().cv_page_limit
    for size in FONT_SIZES:
        data = build_data(db, generated, font_size=size)
        pdf = compile_pdf(data)
        if page_count(pdf) <= limit:
            return pdf, data
    # Still too long at the smallest size: drop bullets from the last projects.
    selection = generated.selection or {}
    caps = {int(pid): len(ids) for pid, ids in (selection.get("bullet_ids") or {}).items()}
    order = list(reversed(selection.get("project_ids") or []))
    while any(count > 1 for count in caps.values()):
        for pid in order:
            if caps.get(pid, 0) > 1:
                caps[pid] -= 1
                break
        data = build_data(db, generated, max_bullets=caps, font_size=FONT_SIZES[-1])
        pdf = compile_pdf(data)
        if page_count(pdf) <= limit:
            data["trimmed_bullets"] = caps
            return pdf, data
    return pdf, data
