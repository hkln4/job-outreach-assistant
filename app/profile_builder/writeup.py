"""The profile write-up (roadmap phase 1, item 8): a long, readable "about me"
document generated from every analyzed record - projects, experience,
skills - with each claim tagged by its record ([E12]). The owner reads and
edits it on the Profile page; it is the base for project selection and
bullets (phase 5) and is never sent anywhere itself.

Tags pointing at records that weren't given are listed back to the owner
(the app, not the model, decides which sources count).
"""
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.applications.evidence import candidate_items, evidence_prompt_section
from app.llm.base import LLMProvider
from app.llm.call_log import llm_context
from app.models import Profile

WRITEUP_CHAR_BUDGET = 80000

WRITEUP_SYSTEM_PROMPT = """You are writing a long, readable "about me" document for the owner \
(a new graduate) from his profile records. It is for HIM to read and edit - a clear inventory of \
what he has actually done - not a cover letter and not marketing.

Structure (Markdown):
# Summary - one paragraph.
# Experience - each role: title, organization, dates, what he did (from the bullets).
# Projects - each project: what it is, the problem, how it works, his contribution, results \
with numbers only where the record has them, technologies and what they were used for.
# Education
# Skills - grouped (languages, frameworks & tools, technologies, generative AI), each with the \
records that show it.
# Open questions - what the records leave unclear and only he can answer.

Rules:
- Use only the records. Never invent a number, a user, a result or a technology.
- Tag every factual sentence with the record it comes from, e.g. "... reached Dice 0.87 [E12]".
- Owner-stated facts ("OWNER STATED") are his own words - use them as facts.
- Write in English, plain and specific.
"""

_TAG_RE = re.compile(r"\[E(\d+)\]")


def unknown_tags(text: str, valid_ids: set[int]) -> list[int]:
    return sorted({int(i) for i in _TAG_RE.findall(text or "")} - valid_ids)


@llm_context("profile_writeup")
def generate_writeup(db: Session, draft_llm: LLMProvider) -> Profile:
    items = candidate_items(db)
    if not items:
        raise ValueError("No analyzed records yet - ingest your sources first.")
    records = evidence_prompt_section(items)
    if len(records) > WRITEUP_CHAR_BUDGET:
        raise ValueError(
            f"The records are {len(records)} characters, over the write-up budget ({WRITEUP_CHAR_BUDGET}). "
            "Skip a few small records first."
        )
    response = draft_llm.generate(
        prompt=f"PROFILE RECORDS:\n{records}",
        system=WRITEUP_SYSTEM_PROMPT,
        temperature=0.2,
        max_output_tokens=8000,
    )
    text = response.text.strip()
    profile = db.query(Profile).first()
    if profile is None:
        profile = Profile()
        db.add(profile)
    profile.writeup = text
    profile.writeup_generated_at = datetime.now(timezone.utc)
    profile.writeup_unknown_tags = unknown_tags(text, {item.id for item in items})
    db.commit()
    db.refresh(profile)
    return profile
