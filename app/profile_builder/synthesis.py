"""Stage 2 synthesis (CLAUDE.md section 3): build the master Profile from all
analyzed ProfileItem records, each claim source-tagged. Never invents beyond what
the item records contain."""
import json

from sqlalchemy.orm import Session

from app.llm.base import LLMProvider
from app.llm.json_utils import parse_json_object
from app.models import Profile, ProfileItem, ProfileItemStatus

SYNTHESIS_SYSTEM_PROMPT = """You are synthesizing one person's profile from a set of \
already-extracted evidence records (CVs, GitHub repos, LinkedIn export, personal sites).

Rules:
- Base every claim only on the given records - do not invent skills, roles, or projects.
- For each skill, cite ONE representative source_ref that best supports it - not every
  record that happens to mention it. Keep evidence_source short (a single source_ref),
  never a comma-separated list.
- Consolidate: list at most ~25 skills, the ones most relevant to the target roles -
  not every individual library or tool mentioned anywhere. Group closely related tools
  under the more general/well-known name rather than listing near-duplicates separately.
- Output ONLY valid JSON, no prose, matching exactly this shape:
{"summary": string, "skills": [{"skill": string, "evidence_source": string}], \
"target_roles": [string]}
"""

SYNTHESIS_FALLBACK = {"summary": "", "skills": [], "target_roles": []}


def synthesize_profile(db: Session, llm: LLMProvider) -> Profile:
    items = db.query(ProfileItem).filter(ProfileItem.status == ProfileItemStatus.ANALYZED).all()
    if not items:
        raise ValueError("No analyzed ProfileItems yet - ingest at least one source first.")

    records_text = "\n\n---\n\n".join(
        f"source_ref: {item.source_ref}\nsource_type: {item.source_type}\n"
        f"extracted: {json.dumps(item.extracted)}"
        for item in items
    )

    response = llm.generate(
        prompt=records_text[:20000],
        system=SYNTHESIS_SYSTEM_PROMPT,
        temperature=0.2,
        # A real profile with many analyzed items needs real room - this used
        # to be 2048, which silently truncated the JSON mid-response for any
        # profile with more than a handful of skills. parse_json_object then
        # fell back to an all-empty profile with no visible error at all.
        max_output_tokens=8192,
    )
    data = parse_json_object(response.text, fallback=SYNTHESIS_FALLBACK)

    if "parse_error" in data:
        # Don't silently save an empty profile over a real one - surface what
        # actually happened so it's visible instead of looking like success.
        raise ValueError(
            "Synthesis produced invalid JSON (likely truncated - try again, or "
            f"reduce the number of analyzed items). Raw response start:\n{data['parse_error']}"
        )

    profile = db.query(Profile).first()
    if profile is None:
        profile = Profile()
        db.add(profile)

    profile.summary = data.get("summary", "")
    profile.skills = data.get("skills", [])
    profile.target_roles = data.get("target_roles", [])
    db.commit()
    db.refresh(profile)
    return profile
