"""The bullet bank (roadmap phase 5). No CV is written per application:
bullets are written once per project, approved by the owner, and every CV is
assembled from them.

- Projects: suggested by code from the profile records (verified results,
  maturity, owner facts, rich role angles); the owner selects / deselects and
  his choice always wins.
- Bullets: 3-6 per project, written by the draft model from the record -
  action verb + what was built + key tech + result. Code checks each one: a
  number that isn't in the record or the owner's facts is flagged (never
  invented to fill the pattern), a technology the record doesn't back is
  flagged, and a bullet without any number asks the owner for a metric.
- Only approved bullets reach a CV. An edited bullet is locked; a later
  regeneration puts its suggestion in `pending_text` as a diff.
"""
import json
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.applications.grounding import grounding_misses, numbers_in
from app.applications.synonyms import ROLE_TYPES
from app.llm.base import LLMProvider
from app.llm.call_log import llm_context, set_llm_ids
from app.llm.json_utils import parse_json_object
from app.models import Bullet, CVProject, ProfileItem, ProfileItemStatus

MAX_BULLETS = 6
MIN_BULLETS = 3
PROJECT_SOURCE_TYPES = ("github_repo", "site", "owner")

BULLET_SYSTEM_PROMPT = f"""You are writing CV bullets for ONE of the owner's projects, from its \
profile record only. These go in a bullet bank the owner approves once and reuses on every CV.

Write {MIN_BULLETS}-{MAX_BULLETS} ATS-friendly bullets in English:
- Pattern: strong action verb + what was built + key technology + result.
- Result: a real metric from RESULTS or OWNER FACTS when one exists; otherwise a concrete scale \
fact the record states (dataset size, rule count, test count, languages supported); otherwise \
no number at all. NEVER invent a number to complete the pattern.
- Technologies only from STACK. One line each when possible (max ~25 words). No first person.
For each bullet return: "text"; "tech" (technologies it names); "domain" (e.g. medical \
imaging, RAG); "role_types" (subset of {list(ROLE_TYPES)} it fits); "source_quote" (the record \
text or owner fact its claim comes from, copied exactly); "source_kind" (result | owner_fact | \
record); "metric" (real | scale | none).
Output ONLY JSON: {{"bullets": [...]}}"""


class _BulletOut(BaseModel):
    text: str
    tech: list[str]
    domain: list[str]
    role_types: list[str]
    source_quote: str
    source_kind: Literal["result", "owner_fact", "record"]
    metric: Literal["real", "scale", "none"]


class BulletsAnswer(BaseModel):
    bullets: list[_BulletOut]


def project_score(item: ProfileItem) -> int:
    """How strong a project looks, by code: verified results, maturity,
    owner facts, stars, rich angles."""
    extracted = item.extracted or {}
    facts = item.facts or {}
    maturity = extracted.get("maturity") or facts.get("maturity") or {}
    score = 10 * len(extracted.get("results") or []) + 5 * len(item.owner_facts or [])
    score += 4 * sum(1 for key in ("tests", "ci", "deployed", "packaged") if maturity.get(key))
    score += min(10, (facts.get("activity") or {}).get("stars", 0))
    score += 2 * sum(1 for angle in (extracted.get("role_angles") or {}).values() if angle and "not relevant" not in angle.lower())
    score += 3 if extracted.get("one_liner") else 0
    share = ((facts.get("activity") or {}).get("owner_commits") or {}).get("share")
    score += 5 if share and share >= 0.8 else 0
    return score


def project_name(item: ProfileItem) -> str:
    extracted = item.extracted or {}
    if item.source_type == "github_repo":
        return item.source_ref.split("/")[-1]
    return extracted.get("title") or extracted.get("one_liner") or extracted.get("purpose", "")[:60] or item.source_ref


def suggest_projects(db: Session, top: int = 5) -> list[CVProject]:
    """Create CVProject rows for analyzed project records; the `top` best
    are selected unless the owner already decided."""
    items = (
        db.query(ProfileItem)
        .filter(ProfileItem.status == ProfileItemStatus.ANALYZED, ProfileItem.source_type.in_(PROJECT_SOURCE_TYPES))
        .all()
    )
    existing = {p.profile_item_id: p for p in db.query(CVProject).all() if p.profile_item_id}
    scored = sorted(items, key=project_score, reverse=True)
    for rank, item in enumerate(scored):
        project = existing.get(item.id)
        if project is None:
            project = CVProject(profile_item_id=item.id, name=project_name(item), link_url=(item.source_urls or [""])[0])
            db.add(project)
        project.score = project_score(item)
        project.sort_order = rank
        if not project.owner_decided:
            project.selected = rank < top
    db.commit()
    return db.query(CVProject).order_by(CVProject.sort_order).all()


def _record_source_text(item: ProfileItem) -> str:
    extracted = {k: v for k, v in (item.extracted or {}).items() if k not in ("purpose", "highlights", "role", "stack")}
    owner = "\n".join(f.get("answer", "") for f in item.owner_facts or [])
    return json.dumps(extracted, ensure_ascii=False) + "\n" + owner


def check_bullet(text: str, item: ProfileItem | None) -> dict:
    """Code-side checks for one bullet against its project's record."""
    if item is None:
        return {"ungrounded_numbers": [], "ungrounded_tech": [], "needs_metric": not numbers_in(text)}
    sources = _record_source_text(item) + "\n" + " ".join((item.facts or {}).get("stack", []))
    misses = grounding_misses(text, sources)
    return {"ungrounded_numbers": misses["numbers"], "ungrounded_tech": misses["technologies"], "needs_metric": not numbers_in(text)}


@llm_context("cv_bullets")
def generate_bullets(db: Session, draft_llm: LLMProvider, project: CVProject) -> list[Bullet]:
    """(Re)write the project's bullets. Approved or locked bullets are kept;
    a locked bullet gets the new suggestion at its position as pending_text."""
    item = project.profile_item
    if item is None:
        raise ValueError("This project has no profile record to write bullets from.")
    set_llm_ids(profile_item_id=item.id)
    extracted = item.extracted or {}
    prompt = (
        f"PROJECT: {project.name}\n"
        f"STACK (from code): {', '.join((item.facts or {}).get('stack') or extracted.get('stack') or [])}\n\n"
        f"RECORD:\n{json.dumps({k: v for k, v in extracted.items() if k not in ('purpose', 'highlights', 'role')}, ensure_ascii=False)[:8000]}\n\n"
        "RESULTS:\n" + "\n".join(f"- {r.get('claim')} (quote: \"{r.get('quote')}\")" for r in extracted.get("results") or []) + "\n\n"
        "OWNER FACTS:\n" + "\n".join(f"- {f.get('answer')}" for f in item.owner_facts or [])
    )
    response = draft_llm.generate(
        prompt=prompt,
        system=BULLET_SYSTEM_PROMPT,
        temperature=0.2,
        max_output_tokens=2000,
        json_schema=BulletsAnswer.model_json_schema(),
    )
    data = parse_json_object(response.text, fallback={})
    if "parse_error" in data:
        raise ValueError("The bullet answer wasn't valid JSON - try again.")
    new = BulletsAnswer.model_validate(data).model_dump()["bullets"][:MAX_BULLETS]

    kept = [b for b in project.bullets if b.locked or b.status == "approved"]
    for bullet in project.bullets:
        if bullet not in kept:
            db.delete(bullet)
    locked_by_order = {b.sort_order: b for b in kept if b.locked}
    for order, out in enumerate(new):
        flags = check_bullet(out["text"], item)
        if order in locked_by_order:
            if out["text"].strip() != locked_by_order[order].text.strip():
                locked_by_order[order].pending_text = out["text"]
            continue
        if any(b.text.strip() == out["text"].strip() for b in kept):
            continue
        db.add(
            Bullet(
                project_id=project.id,
                text=out["text"].strip(),
                tags={"tech": out["tech"], "domain": out["domain"], "role_types": [r for r in out["role_types"] if r in ROLE_TYPES]},
                source={"kind": out["source_kind"], "quote": out["source_quote"], "metric": out["metric"]},
                status="draft",
                flags=flags,
                sort_order=order,
            )
        )
    db.commit()
    db.refresh(project)
    return project.bullets


def edit_bullet(db: Session, bullet: Bullet, text: str) -> None:
    """The owner's words: locked from now on, checks re-run."""
    if text.strip() and text.strip() != bullet.text:
        bullet.text = text.strip()
        bullet.locked = True
        bullet.pending_text = ""
        bullet.flags = check_bullet(bullet.text, bullet.project.profile_item if bullet.project else None)
        db.commit()
