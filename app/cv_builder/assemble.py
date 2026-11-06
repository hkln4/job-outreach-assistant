"""Per-application CV assembly (roadmap phase 5) - mostly code.

- Locked sections: Education and Professional Experience are copied verbatim
  from the base CV's entry records (never re-selected or reworded);
  Certificates are one line each (names and issuers), off by default, on when
  the posting asks for certifications or the owner enables them.
- Skills: posting keywords matched to the tech list through the synonym
  table; matched items first in each group; unevidenced or
  evidence-removed skills left out unless the owner kept them by hand.
- Projects: the selected ones ranked by tag overlap with the posting (or the
  role type for an open application); top 3; approved bullets only.
- Keyword report: covered vs missing. Missing keywords are reported as gaps
  and never added anywhere.
"""
from sqlalchemy.orm import Session

from app.applications.grounding import technologies_in
from app.applications.synonyms import ROLE_CONCEPTS, concepts_in, fold, role_type_for, tokens
from app.cv_builder.skills import GROUPS, by_importance, evidence_status, selected_project_item_ids
from app.models import CV, CVProject, JobPosting, OwnerSettings, ProfileItem, TechSkill

MAX_PROJECTS = 3
MAX_BULLETS_PER_PROJECT = 4
MAX_SKILLS_PER_GROUP = {"languages": 6, "frameworks": 22, "technologies": 14, "genai": 10}
CERT_WORDS = ("certif", "sertifika", "certificate")


def base_cv(db: Session) -> CV | None:
    settings = db.query(OwnerSettings).first()
    if settings and settings.base_cv_id:
        cv = db.get(CV, settings.base_cv_id)
        if cv is not None:
            return cv
    for cv in db.query(CV).filter(CV.file_path != "").order_by(CV.id).all():
        if db.query(ProfileItem).filter(ProfileItem.parent_ref == cv.file_path).count():
            return cv
    return None


def locked_sections(db: Session) -> dict:
    """{education[], experience[], certificates[], skills_items[]} verbatim
    from the base CV's entries, in document order."""
    cv = base_cv(db)
    sections = {"education": [], "experience": [], "certificates": [], "base_cv": cv.name if cv else ""}
    if cv is None:
        return sections
    items = db.query(ProfileItem).filter(ProfileItem.parent_ref == cv.file_path).order_by(ProfileItem.id).all()
    for item in items:
        e = item.extracted or {}
        entry = {
            "title": e.get("title", ""), "organization": e.get("organization", ""), "start": e.get("start", ""),
            "end": e.get("end", ""), "location": e.get("location", ""), "bullets": list(e.get("bullets") or []),
        }
        kind = e.get("entry_type")
        if kind == "education":
            sections["education"].append(entry)
        elif kind == "experience":
            sections["experience"].append(entry)
        elif kind == "certificate":
            sections["certificates"].append({"title": entry["title"], "organization": entry["organization"]})
    return sections


def posting_keywords(posting: JobPosting | None, role_type: str) -> list[str]:
    """What the CV should cover: technologies and concepts the posting names,
    or the role type's concepts for an open application."""
    if posting is None:
        return sorted(ROLE_CONCEPTS.get(role_type, set()))
    text = " ".join([posting.title, *posting.requirements, *posting.nice_to_have, posting.description_text[:4000]])
    found = technologies_in(text) + sorted(concepts_in(" ".join([posting.title, *posting.requirements, *posting.nice_to_have])))
    return list(dict.fromkeys(found))


def _matches(text: str, keywords: list[str]) -> set[str]:
    folded = fold(text)
    concepts = concepts_in(text)
    return {k for k in keywords if fold(k) in folded or k in concepts}


def assemble(db: Session, posting: JobPosting | None = None, company=None, certificates: bool | None = None, include_drafts: bool = False) -> dict:
    """A selection for a GeneratedCV (ids and names only - the text comes
    from the bank and the base CV at render time)."""
    research = (company.research or {}) if company is not None else {}
    role_type = role_type_for(
        " ".join([posting.title if posting else "", research.get("summary") or ""] + [p.get("title", "") for p in research.get("open_positions") or []])
    )
    keywords = posting_keywords(posting, role_type)
    selected_ids = selected_project_item_ids(db)

    skills: dict[str, list[str]] = {}
    for group in GROUPS:
        candidates = by_importance([
            s for s in db.query(TechSkill).filter(TechSkill.group == group, TechSkill.hidden.is_(False)).all()
            if evidence_status(s, selected_ids) == "ok" or s.manual
        ])
        matched = [s.name for s in candidates if _matches(s.name, keywords)]
        # general skills always; a niche tool only when the posting asks for it
        rest = [s.name for s in candidates if s.name not in matched and s.tier == "core"]
        skills[group] = (matched + rest)[: MAX_SKILLS_PER_GROUP[group]]

    projects = []
    for project in db.query(CVProject).filter(CVProject.selected.is_(True)).all():
        approved = [b for b in project.bullets if b.status == "approved" or include_drafts]
        if not approved:
            continue
        item = project.profile_item
        tag_text = " ".join(
            [project.name]
            + [" ".join(b.tags.get("tech", []) + b.tags.get("domain", [])) for b in approved]
            + ((item.facts or {}).get("stack", []) if item else [])
            + ((item.extracted or {}).get("domains", []) if item else [])
        )
        role_fit = sum(role_type in (b.tags.get("role_types") or []) for b in approved)
        overlap = len(_matches(tag_text, keywords)) + len(tokens(tag_text) & tokens(" ".join(keywords)))
        ranked_bullets = sorted(approved, key=lambda b: (-len(_matches(b.text + " " + " ".join(b.tags.get("tech", [])), keywords)), b.sort_order))
        projects.append((overlap * 3 + role_fit + project.score / 100, project, ranked_bullets[:MAX_BULLETS_PER_PROJECT]))
    projects.sort(key=lambda entry: -entry[0])
    chosen = projects[:MAX_PROJECTS]

    if certificates is None:
        certificates = bool(posting) and any(word in fold(posting.description_text) for word in CERT_WORDS)
    return {
        "role_type": role_type,
        "keywords": keywords,
        "skills": skills,
        "project_ids": [p.id for _, p, _ in chosen],
        "bullet_ids": {str(p.id): [b.id for b in bullets] for _, p, bullets in chosen},
        "bullet_overrides": {},
        "certificates": certificates,
        "include_drafts": include_drafts,
    }


def keyword_report(keywords: list[str], cv_text: str) -> dict:
    """Covered vs missing - missing ones are gaps, never added to the CV."""
    covered = sorted(_matches(cv_text, keywords))
    return {"covered": covered, "missing": [k for k in keywords if k not in covered]}
