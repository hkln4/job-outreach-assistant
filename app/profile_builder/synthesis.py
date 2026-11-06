"""Stage 2 synthesis (CLAUDE.md section 3): build the master Profile from all
analyzed ProfileItem records, each claim source-tagged. Never invents beyond what
the item records contain.

Completeness is the app's job: when the records don't fit one prompt, synthesis
is hierarchical instead of truncated - records are grouped by source type and
packed into chunks that fit, each chunk is synthesized into a partial profile,
and the partials are merged (in rounds, if even they don't fit). Every analyzed
record reaches some synthesis call; none is cut off the end of a prompt.
"""
import json

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.companies.fetch import _trim_keep_head_and_tail
from app.llm.base import LLMProvider
from app.llm.call_log import llm_context
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

MERGE_SYSTEM_PROMPT = """You are merging several PARTIAL profiles of the same person into \
one. Each partial was synthesized from a different subset of their evidence records.

Rules:
- Use only what the partials contain - never add skills, roles or claims.
- summary: one coherent summary covering all partials (no repetition).
- skills: deduplicate across partials, keep each skill's evidence_source exactly as given \
(one source_ref), at most ~25 skills, most relevant to the target roles first.
- target_roles: the union, deduplicated, most supported first.
- Output ONLY valid JSON, this exact shape:
{"summary": string, "skills": [{"skill": string, "evidence_source": string}], \
"target_roles": [string]}
"""

SYNTHESIS_FALLBACK = {"summary": "", "skills": [], "target_roles": []}
# One synthesis prompt's budget for record text (cost discipline, section 8).
SYNTHESIS_CHAR_BUDGET = 20000
RECORD_SEPARATOR = "\n\n---\n\n"


class _Skill(BaseModel):
    skill: str
    evidence_source: str


class SynthesisAnswer(BaseModel):
    summary: str
    skills: list[_Skill]
    target_roles: list[str]


def _record_text(item: ProfileItem) -> str:
    text = (
        f"source_ref: {item.source_ref}\nsource_type: {item.source_type}\n"
        f"extracted: {json.dumps(item.extracted, ensure_ascii=False)}"
    )
    # A single oversized record is shortened, never dropped: its source_ref
    # header always survives.
    return _trim_keep_head_and_tail(text, SYNTHESIS_CHAR_BUDGET)


def _chunks(texts: list[str], budget: int) -> list[list[str]]:
    """Pack texts in order into chunks whose joined length fits `budget`."""
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for text in texts:
        added = len(text) + (len(RECORD_SEPARATOR) if current else 0)
        if current and size + added > budget:
            chunks.append(current)
            current, size = [], 0
            added = len(text)
        current.append(text)
        size += added
    if current:
        chunks.append(current)
    return chunks


def _call(llm: LLMProvider, prompt: str, system: str) -> dict:
    response = llm.generate(
        prompt=prompt,
        system=system,
        temperature=0.2,
        # A real profile with many analyzed items needs real room - this used
        # to be 2048, which silently truncated the JSON mid-response for any
        # profile with more than a handful of skills. parse_json_object then
        # fell back to an all-empty profile with no visible error at all.
        max_output_tokens=8192,
        json_schema=SynthesisAnswer.model_json_schema(),
    )
    data = parse_json_object(response.text, fallback=SYNTHESIS_FALLBACK)
    if "parse_error" in data:
        # Don't silently save an empty profile over a real one - surface what
        # actually happened so it's visible instead of looking like success.
        raise ValueError(
            "Synthesis produced invalid JSON (likely truncated - try again). "
            f"Raw response start:\n{data['parse_error']}"
        )
    return data


def _merge(llm: LLMProvider, partials: list[dict]) -> dict:
    """Merge partial profiles, in rounds while they don't fit one prompt."""
    while len(partials) > 1:
        texts = [json.dumps(partial, ensure_ascii=False) for partial in partials]
        groups = _chunks(texts, SYNTHESIS_CHAR_BUDGET)
        if len(groups) == len(texts) and len(texts) > 1:
            groups = [texts[i : i + 2] for i in range(0, len(texts), 2)]  # always make progress
        partials = [
            _call(llm, "PARTIAL PROFILES:\n" + RECORD_SEPARATOR.join(group), MERGE_SYSTEM_PROMPT)
            for group in groups
        ]
    return partials[0]


@llm_context("profile_synthesis")
def synthesize_profile(db: Session, llm: LLMProvider) -> Profile:
    items = (
        db.query(ProfileItem)
        .filter(ProfileItem.status == ProfileItemStatus.ANALYZED)
        .order_by(ProfileItem.source_type, ProfileItem.id)
        .all()
    )
    if not items:
        raise ValueError("No analyzed ProfileItems yet - ingest at least one source first.")

    texts = [_record_text(item) for item in items]
    chunks = _chunks(texts, SYNTHESIS_CHAR_BUDGET)
    if len(chunks) == 1:
        data = _call(llm, RECORD_SEPARATOR.join(chunks[0]), SYNTHESIS_SYSTEM_PROMPT)
    else:
        # Items are ordered by source type, so chunks follow the groups.
        partials = [_call(llm, RECORD_SEPARATOR.join(chunk), SYNTHESIS_SYSTEM_PROMPT) for chunk in chunks]
        data = _merge(llm, partials)

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
