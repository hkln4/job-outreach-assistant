"""Fact capture (roadmap phase 4, item 4): facts the owner states about
himself outside ingestion - in reject feedback ("I also did a 6-month
internship at X") or in a draft edit - must reach the profile, or the
grounding checks will treat them as invented next time.

Code decides whether there is anything to look at (first-person statements,
numbers, experience words); only then the reading model lists candidate
facts, each with the owner's own words as a quote (checked by code). They are
stored as ProfileFact proposals; nothing enters the profile without the
owner's approval, which turns a fact into a ProfileItem (source "owner") -
an evidence record like any other.
"""
import re

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.applications.draft_review import quote_in_draft
from app.applications.synonyms import fold
from app.llm.base import LLMProvider
from app.llm.call_log import llm_context
from app.llm.json_utils import parse_json_object
from app.models import ProfileFact, ProfileFactStatus, ProfileItem, ProfileItemStatus
from app.profile_builder.coverage import record_item

FACT_HINT_RE = re.compile(
    r"\b(i|i've|i'm|my|me|ben|benim|bende|yaptım|yaptim|çalıştım|calistim|staj|intern(ship)?|worked|built|"
    r"geliştirdim|gelistirdim|kullandım|kullandim|mezun|degree|certificate|sertifika|years?|yıl|ay|months?)\b|\d",
    re.IGNORECASE,
)

FACT_SYSTEM_PROMPT = """The owner wrote the TEXT below while reviewing a job application email \
(feedback or an edit). List the facts he states ABOUT HIMSELF - experience, roles, projects, \
skills, results, education - that the KNOWN RECORDS don't already contain. Ignore instructions \
about wording or style. For each: "text" (the fact as one plain English sentence) and "quote" \
(his own words it comes from, copied exactly). Empty list if there are none.
Output ONLY JSON: {"facts": [{"text": string, "quote": string}]}"""


class _Fact(BaseModel):
    text: str
    quote: str


class FactsAnswer(BaseModel):
    facts: list[_Fact]


def worth_checking(text: str) -> bool:
    return bool(text and FACT_HINT_RE.search(text))


@llm_context("fact_capture")
def propose_facts(db: Session, llm: LLMProvider, text: str, *, source_kind: str, application_id: int | None, known: str) -> list[ProfileFact]:
    if not worth_checking(text):
        return []
    try:
        response = llm.generate(
            prompt=f"KNOWN RECORDS:\n{known[:12000]}\n\nTEXT:\n{text[:4000]}",
            system=FACT_SYSTEM_PROMPT,
            temperature=0.0,
            max_output_tokens=800,
            json_schema=FactsAnswer.model_json_schema(),
        )
        data = parse_json_object(response.text, fallback={"facts": []})
        facts = FactsAnswer.model_validate(data).facts if "parse_error" not in data else []
    except Exception:  # noqa: BLE001 - fact capture is a help; feedback still applies
        return []
    existing = {fold(f.text).strip() for f in db.query(ProfileFact).all()}
    created = []
    for fact in facts:
        if not quote_in_draft(fact.quote, "", text) or fold(fact.text).strip() in existing:
            continue
        proposal = ProfileFact(text=fact.text.strip(), quote=fact.quote.strip(), source_kind=source_kind, application_id=application_id)
        db.add(proposal)
        created.append(proposal)
    db.commit()
    return created


def approve_fact(db: Session, fact: ProfileFact, text: str | None = None) -> ProfileItem:
    """The fact becomes an evidence record (ProfileItem, source "owner")."""
    statement = (text or fact.text).strip()
    item = record_item(
        db,
        source_type="owner",
        source_ref=f"owner-fact:{fact.id}",
        extracted={"purpose": statement, "stack": [], "role": "owner-stated", "highlights": [statement]},
        source_urls=[],
    )
    item.owner_facts = [{"question": "", "answer": statement, "source": "owner"}]
    item.status = ProfileItemStatus.ANALYZED
    fact.text = statement
    fact.status = ProfileFactStatus.APPROVED
    fact.profile_item_id = item.id
    db.commit()
    return item
