"""CV and LinkedIn exports as one record per entry (roadmap phase 1, item 6).

A CV file used to become one blob record. Now the reading model splits it
into its entries - each role, education, project, certificate - and each
becomes its own ProfileItem (`parent_ref` = the file), so evidence retrieval
can pick "the internship at X" instead of "the CV". Dates, employer, title and
bullet text are kept verbatim; code checks that: a bullet that isn't found in
the file's text is moved to `unverified_bullets`, and a technology the file
doesn't name is dropped into `claims_to_verify`.

Entry refs are stable (file + type + title + organization), so re-ingesting a
file updates the same records and the owner's edits and answers survive.
Entries that disappeared from the file are deleted - unless the owner edited
or annotated them, then they are kept as `skipped`.
"""
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.llm.base import LLMProvider
from app.llm.call_log import llm_context
from app.llm.json_utils import parse_json_object
from app.models import ProfileItem, ProfileItemStatus
from app.profile_builder.coverage import record_item
from app.profile_builder.extraction import EXTRACTION_FALLBACK

MAX_DOCUMENT_CHARS = 30000
ENTRY_TYPES = ("experience", "education", "project", "certificate", "volunteering", "award", "skills", "other")

ENTRIES_SYSTEM_PROMPT = """You are splitting ONE document about a person (a CV or a LinkedIn \
profile export) into its entries: each job/internship, each education, each project, each \
certificate, etc. One entry per item - never merge two roles.

Rules:
- Copy text VERBATIM from the document: title, organization, dates, location and every bullet \
exactly as written (same language, same wording, same numbers). Never rephrase or translate.
- start / end: as written ("Sep 2024", "2023", "Present"); "" if absent.
- bullets: the entry's bullet points / description sentences, verbatim, one per item.
- stack: technologies the entry itself names, as written. Never add any.
- purpose: one plain English sentence on what the entry is (this is the only field you write).
- A "skills" section becomes one entry of type "skills" with its items as bullets.
- Output ONLY JSON: {"entries": [{"entry_type": string, "title": string, "organization": string, \
"start": string, "end": string, "location": string, "bullets": [string], "stack": [string], \
"purpose": string}]}
"""


class _Entry(BaseModel):
    entry_type: Literal[ENTRY_TYPES]
    title: str
    organization: str
    start: str
    end: str
    location: str
    bullets: list[str]
    stack: list[str]
    purpose: str


class EntriesAnswer(BaseModel):
    entries: list[_Entry]


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("’", "'").replace("“", '"').replace("”", '"').replace("–", "-").replace("—", "-")
    text = re.sub(r"^[\s•\-\*●▪◦·]+", "", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def _found(snippet: str, haystack: str) -> bool:
    needle = _normalize(snippet)
    # PDF text layers drop hyphens at line breaks ("mid-LTV" -> "midLTV"): also
    # accept a match with hyphens and spaces removed on both sides.
    squeeze = lambda s: re.sub(r"[\s\-]", "", s)  # noqa: E731
    return bool(needle) and (needle in haystack or squeeze(needle) in squeeze(haystack))


def _slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50] or "entry"


def entry_ref(parent: str, entry: dict) -> str:
    return f"{parent}#{entry['entry_type']}:{_slug(entry.get('title', ''))}@{_slug(entry.get('organization', ''))}"


def verify_entry(entry: dict, document_normalized: str) -> dict:
    entry = dict(entry)
    bullets, unverified = [], []
    for bullet in entry.get("bullets") or []:
        (bullets if _found(bullet, document_normalized) else unverified).append(bullet)
    entry["bullets"] = bullets
    entry["unverified_bullets"] = unverified
    stack, claims = [], []
    for tech in entry.get("stack") or []:
        if _found(tech, document_normalized):
            stack.append(tech)
        else:
            claims.append(f"Technology not found in the document: {tech}")
    entry["stack"] = stack
    entry["claims_to_verify"] = claims
    # v1 mirrors for older readers
    entry["role"] = " @ ".join(part for part in (entry.get("title"), entry.get("organization")) if part) or "unknown"
    entry["highlights"] = bullets
    entry["purpose"] = entry.get("purpose") or "unknown"
    return entry


@llm_context("profile_entries")
def extract_entries(llm: LLMProvider, text: str) -> list[dict]:
    response = llm.generate(
        prompt=text[:MAX_DOCUMENT_CHARS],
        system=ENTRIES_SYSTEM_PROMPT,
        temperature=0.0,
        max_output_tokens=8000,
        json_schema=EntriesAnswer.model_json_schema(),
    )
    data = parse_json_object(response.text, fallback={})
    if "parse_error" in data:
        raise ValueError(f"The entries answer wasn't valid JSON: {data['parse_error'][:200]}")
    return EntriesAnswer.model_validate(data).model_dump()["entries"]


def ingest_document_entries(db: Session, llm: LLMProvider, path: str, text: str | None, error: str | None, source_type: str) -> list[ProfileItem]:
    """One record per entry of a CV / LinkedIn file. A failure becomes one
    needs_review record for the whole file (source_ref = the file)."""
    entries: list[dict] = []
    if error is None:
        try:
            entries = extract_entries(llm, text or "")
        except Exception as exc:  # noqa: BLE001 - recorded as needs_review
            error = str(exc)
    if error is not None or not entries:
        item = record_item(
            db,
            source_type=source_type,
            source_ref=path,
            extracted={**EXTRACTION_FALLBACK, "parse_error": error or "No entries found in the document."},
            source_urls=[],
        )
        return [item]

    document = _normalize(text or "")
    refs, items = [], []
    for entry in entries:
        ref = entry_ref(path, entry)
        if ref in refs:  # two entries with the same title at the same place
            ref = f"{ref}-{len(refs)}"
        refs.append(ref)
        items.append(
            record_item(
                db,
                source_type=source_type,
                source_ref=ref,
                extracted=verify_entry(entry, document),
                source_urls=[],
                parent_ref=path,
            )
        )

    # the whole-file record from before entries existed, and entries that are gone
    stale = (
        db.query(ProfileItem)
        .filter(ProfileItem.source_type == source_type)
        .filter((ProfileItem.parent_ref == path) | (ProfileItem.source_ref == path))
        .all()
    )
    for item in stale:
        if item.source_ref in refs:
            continue
        if item.owner_edited_fields or item.owner_facts:
            item.status = ProfileItemStatus.SKIPPED
        else:
            db.delete(item)
    db.commit()
    return items
