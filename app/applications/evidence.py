"""Profile evidence retrieval for drafting: picks the owner's few background
records (ProfileItems - one per repo / CV / LinkedIn export / site) that are
most relevant to one company, so the letter can name real projects instead of
paraphrasing the profile summary.

Selection is a small LLM call over a compact one-line-per-record index, not
keyword matching: company text is usually Turkish while the records are
English, and "bilgisayarlı görü" has to find a "computer vision" repo. The app
still owns the candidate set (CLAUDE.md section 9) - the model only ranks ids
it was given, and anything else it returns is dropped.
"""
import json
import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.llm.base import LLMProvider
from app.llm.json_utils import parse_json_object
from app.llm.untrusted import UNTRUSTED_RULE, wrap_untrusted
from app.models import Company, ProfileItem, ProfileItemStatus

logger = logging.getLogger("app.evidence")

MAX_EVIDENCE = 8
MAX_PURPOSE_CHARS = 160
MAX_STACK_IN_INDEX = 6
# Per-record budget in the drafting prompt (cost discipline, section 8).
MAX_RECORD_CHARS = 1500

SELECT_SYSTEM_PROMPT = f"""You are choosing which of a job applicant's background records \
are the strongest evidence for an application to one company.

Rules:
- Choose at most {MAX_EVIDENCE} record ids from the given RECORDS list, most relevant first.
- Relevant means: the record's stack, domain, or purpose matches what the company does, \
its open positions, or the owner's chosen position/reason. Match by meaning, across \
languages (company text may be Turkish, records are English).
- Only use ids that appear in the list. Never invent ids.
- For each chosen id, give a short reason (one sentence, English).
- Output ONLY valid JSON, this exact shape:
{{"item_ids": [int], "why": {{"<id>": string}}}}
""" + UNTRUSTED_RULE


@dataclass
class EvidenceSelection:
    items: list[ProfileItem]
    why: dict[int, str] = field(default_factory=dict)
    # True when the selection call failed and a CV/LinkedIn-only default was used.
    fallback: bool = False
    # Why it failed - shown on the review page instead of failing silently.
    error: str = ""


def _index_line(item: ProfileItem) -> str:
    extracted = item.extracted or {}
    stack = ", ".join((extracted.get("stack") or [])[:MAX_STACK_IN_INDEX]) or "unknown"
    purpose = (extracted.get("purpose") or "unknown")[:MAX_PURPOSE_CHARS]
    return f"E{item.id} | {item.source_type} | {item.source_ref} | stack: {stack} | {purpose}"


def _company_context(company: Company, target_position: str, application_reason: str) -> str:
    research = company.research or {}
    positions = "\n".join(
        f"- {p.get('title', '')}: {p.get('details', '')}".strip()
        for p in research.get("open_positions") or []
    )
    company_part = (
        f"SUMMARY: {research.get('summary') or 'unknown'}\n"
        f"OPEN POSITIONS:\n{positions or 'none listed'}"
    )
    owner_part = ""
    if target_position.strip():
        owner_part += f"\nOWNER'S CHOSEN POSITION: {target_position.strip()}"
    if application_reason.strip():
        owner_part += f"\nOWNER'S REASON FOR APPLYING: {application_reason.strip()}"
    if company.why_interested.strip():
        owner_part += f"\nOWNER'S NOTES ON WHY THIS COMPANY: {company.why_interested.strip()}"
    return f"COMPANY: {company.name}\n{wrap_untrusted(company_part)}{owner_part}"


def _parse_ids(raw: object) -> list[int]:
    ids: list[int] = []
    for value in raw if isinstance(raw, list) else []:
        try:
            ids.append(int(str(value).strip().lstrip("Ee")))
        except ValueError:
            continue
    return ids


def candidate_items(db: Session, exclude_ids: list[int] | tuple = ()) -> list[ProfileItem]:
    """Analyzed records, one per source (the newest, if a source was ingested
    more than once before re-ingestion started updating in place)."""
    query = db.query(ProfileItem).filter(ProfileItem.status == ProfileItemStatus.ANALYZED)
    if exclude_ids:
        query = query.filter(ProfileItem.id.notin_(list(exclude_ids)))
    newest: dict[tuple[str, str], ProfileItem] = {}
    for item in query.order_by(ProfileItem.id).all():
        newest[(item.source_type, item.source_ref)] = item
    return sorted(newest.values(), key=lambda item: item.id)


def _ensure_a_cv(chosen: list[ProfileItem], candidates: list[ProfileItem]) -> list[ProfileItem]:
    """Work experience lives in the CV records - never let a draft go without
    one, even if the ranking preferred repos."""
    if any(item.source_type == "cv" for item in chosen):
        return chosen
    cvs = [item for item in candidates if item.source_type == "cv"]
    return chosen + cvs[-1:]


def _fallback(candidates: list[ProfileItem], error: str) -> EvidenceSelection:
    logger.warning("Evidence selection failed, using CV/LinkedIn records only: %s", error)
    items = [item for item in candidates if item.source_type in ("cv", "linkedin")][:MAX_EVIDENCE]
    return EvidenceSelection(items=items, fallback=True, error=error)


def select_evidence(
    db: Session,
    llm: LLMProvider,
    company: Company,
    target_position: str = "",
    application_reason: str = "",
    exclude_ids: list[int] | tuple = (),
) -> EvidenceSelection:
    candidates = candidate_items(db, exclude_ids)
    if len(candidates) <= MAX_EVIDENCE:
        return EvidenceSelection(items=candidates)  # nothing to choose between - skip the call

    index = "\n".join(_index_line(item) for item in candidates)
    prompt = f"RECORDS:\n{index}\n\n{_company_context(company, target_position, application_reason)}"
    try:
        response = llm.generate(prompt=prompt, system=SELECT_SYSTEM_PROMPT, temperature=0.0, max_output_tokens=800)
    except Exception as exc:  # noqa: BLE001 - a failed ranking must not block drafting
        return _fallback(candidates, f"{type(exc).__name__}: {exc}"[:300])
    data = parse_json_object(response.text, fallback={})
    if "parse_error" in data:
        return _fallback(candidates, f"the answer wasn't valid JSON: {data['parse_error'][:200]}")

    by_id = {item.id: item for item in candidates}
    chosen: list[ProfileItem] = []
    for item_id in _parse_ids(data.get("item_ids")):
        if item_id in by_id and by_id[item_id] not in chosen:
            chosen.append(by_id[item_id])
    chosen = chosen[:MAX_EVIDENCE]
    if not chosen:
        return _fallback(candidates, "the answer named none of the offered record ids")

    raw_why = data.get("why") if isinstance(data.get("why"), dict) else {}
    why = {}
    for key, reason in raw_why.items():
        ids = _parse_ids([key])
        if ids and ids[0] in by_id and isinstance(reason, str):
            why[ids[0]] = reason.strip()
    return EvidenceSelection(items=_ensure_a_cv(chosen, candidates), why=why)


def items_by_ids(db: Session, ids: list[int], exclude_ids: list[int] | tuple = ()) -> list[ProfileItem]:
    """Reload a saved selection, in its saved order, skipping records deleted
    or excluded since."""
    wanted = [i for i in ids if i not in set(exclude_ids)]
    found = {item.id: item for item in candidate_items(db) if item.id in wanted}
    return [found[i] for i in wanted if i in found]


def evidence_prompt_section(items: list[ProfileItem]) -> str:
    blocks = []
    for item in items:
        record = json.dumps(item.extracted or {}, ensure_ascii=False)[:MAX_RECORD_CHARS]
        urls = ", ".join(item.source_urls or []) or "none"
        blocks.append(f"[E{item.id}] {item.source_type} | {item.source_ref} | urls: {urls}\n{record}")
    return "\n\n".join(blocks)


def evidence_records(selection_items: list[ProfileItem], why: dict[int, str], used_ids: list[int]) -> list[dict]:
    """What is stored on the Application and shown at review: every retrieved
    record, and whether the draft actually drew on it."""
    return [
        {
            "item_id": item.id,
            "source_type": item.source_type,
            "source_ref": item.source_ref,
            "source_url": (item.source_urls or [None])[0],
            "purpose": ((item.extracted or {}).get("purpose") or "")[:MAX_PURPOSE_CHARS],
            "why": why.get(item.id, ""),
            "used": item.id in used_ids,
        }
        for item in selection_items
    ]
