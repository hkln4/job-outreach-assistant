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
import re
from dataclasses import dataclass, field

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.applications.synonyms import concepts_in, tokens
from app.llm.base import LLMProvider
from app.llm.call_log import llm_context
from app.llm.json_utils import parse_json_object
from app.llm.untrusted import UNTRUSTED_RULE, wrap_untrusted
from app.models import Company, ProfileItem, ProfileItemStatus

logger = logging.getLogger("app.evidence")

MAX_EVIDENCE = 8
MAX_PURPOSE_CHARS = 160
MAX_STACK_IN_INDEX = 10
# Code pre-rank keeps this many records for the model to rank (section 8:
# deterministic first, the LLM only sees what survives).
PRE_RANK_LIMIT = 20
# Per-record budget in the drafting prompt (cost discipline, section 8).
MAX_RECORD_CHARS = 2500

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
{{"item_ids": [int], "reasons": [{{"item_id": int, "why": string}}]}}
""" + UNTRUSTED_RULE


class _Reason(BaseModel):
    item_id: int
    why: str


# Answer shape, sent as the structured-output schema (a comment, not a
# docstring: docstrings end up in the schema the model reads). Reasons are a
# list, not an id-keyed object - strict schemas can't describe free-form keys.
class SelectionAnswer(BaseModel):
    item_ids: list[int]
    reasons: list[_Reason] = []


@dataclass
class EvidenceSelection:
    items: list[ProfileItem]
    why: dict[int, str] = field(default_factory=dict)
    # True when the selection call failed and a CV/LinkedIn-only default was used.
    fallback: bool = False
    # Why it failed - shown on the review page instead of failing silently.
    error: str = ""


def record_stack(item: ProfileItem) -> list[str]:
    """The deterministic stack when the record has facts, else the record's own."""
    return list((item.facts or {}).get("stack") or (item.extracted or {}).get("stack") or [])


def record_title(item: ProfileItem) -> str:
    extracted = item.extracted or {}
    if extracted.get("one_liner"):
        return extracted["one_liner"]
    if extracted.get("entry_type"):
        dates = " - ".join(part for part in (extracted.get("start"), extracted.get("end")) if part)
        return f"{extracted.get('entry_type')}: {extracted.get('role', '')}" + (f" ({dates})" if dates else "")
    return extracted.get("purpose") or "unknown"


def _index_line(item: ProfileItem) -> str:
    extracted = item.extracted or {}
    stack = ", ".join(record_stack(item)[:MAX_STACK_IN_INDEX]) or "unknown"
    domains = ", ".join(extracted.get("domains") or [])
    line = f"E{item.id} | {item.source_type} | {item.source_ref} | {record_title(item)[:MAX_PURPOSE_CHARS]} | stack: {stack}"
    return line + (f" | domains: {domains}" if domains else "")


def _record_text(item: ProfileItem) -> str:
    """Everything the pre-rank may match on."""
    extracted = item.extracted or {}
    parts = [record_title(item), extracted.get("purpose", ""), extracted.get("problem", ""), extracted.get("approach", "")]
    parts += record_stack(item) + list(extracted.get("domains") or []) + list(extracted.get("bullets") or [])
    parts += [use.get("tech", "") + " " + use.get("for", "") for use in extracted.get("stack_used_for") or []]
    parts += [fact.get("answer", "") for fact in item.owner_facts or []]
    return " ".join(str(p) for p in parts if p)


def pre_rank(candidates: list[ProfileItem], query: str, limit: int = PRE_RANK_LIMIT) -> list[ProfileItem]:
    """Cheap lexical + concept overlap (bilingual synonym table), no LLM.
    CV/LinkedIn records always stay in (work experience lives there)."""
    if len(candidates) <= limit:
        return candidates
    query_concepts = concepts_in(query)
    query_tokens = tokens(query)

    def score(item: ProfileItem) -> tuple[float, int]:
        text = _record_text(item)
        concept_hits = len(concepts_in(text) & query_concepts)
        token_hits = len(tokens(text) & query_tokens)
        return (3 * concept_hits + token_hits, item.id)

    always = [item for item in candidates if item.source_type in ("cv", "linkedin")][: limit // 2]
    ranked = sorted((item for item in candidates if item not in always), key=score, reverse=True)
    kept = always + ranked[: limit - len(always)]
    return sorted(kept, key=lambda item: item.id)


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
    one, even if the ranking preferred repos. With per-entry CV records, the
    experience entries are what's added."""
    if any(item.source_type == "cv" for item in chosen):
        return chosen
    cvs = [item for item in candidates if item.source_type == "cv"]
    experience = [item for item in cvs if (item.extracted or {}).get("entry_type") == "experience"]
    return chosen + (experience[:2] or cvs[-1:])


def _fallback(candidates: list[ProfileItem], error: str) -> EvidenceSelection:
    logger.warning("Evidence selection failed, using CV/LinkedIn records only: %s", error)
    items = [item for item in candidates if item.source_type in ("cv", "linkedin")][:MAX_EVIDENCE]
    return EvidenceSelection(items=items, fallback=True, error=error)


@llm_context("evidence_selection")
def select_evidence(
    db: Session,
    llm: LLMProvider,
    company: Company,
    target_position: str = "",
    application_reason: str = "",
    exclude_ids: list[int] | tuple = (),
    job_posting=None,
) -> EvidenceSelection:
    all_candidates = candidate_items(db, exclude_ids)
    if len(all_candidates) <= MAX_EVIDENCE:
        return EvidenceSelection(items=all_candidates)  # nothing to choose between - skip the call
    research = company.research or {}
    query = " ".join(
        [research.get("summary") or "", target_position, application_reason, company.why_interested or ""]
        + [f"{p.get('title', '')} {p.get('details', '')}" for p in research.get("open_positions") or []]
        + ([job_posting.title, job_posting.description_text or ""] if job_posting is not None else [])
    )
    candidates = pre_rank(all_candidates, query)

    index = "\n".join(_index_line(item) for item in candidates)
    prompt = f"RECORDS:\n{index}\n\n{_company_context(company, target_position, application_reason)}"
    if job_posting is not None:
        prompt += f"\n\nJOB POSTING ({job_posting.title}):\n{wrap_untrusted(job_posting.description_text[:6000], label='JOB POSTING')}"
    try:
        response = llm.generate(
            prompt=prompt,
            system=SELECT_SYSTEM_PROMPT,
            temperature=0.0,
            max_output_tokens=800,
            json_schema=SelectionAnswer.model_json_schema(),
        )
    except Exception as exc:  # noqa: BLE001 - a failed ranking must not block drafting
        return _fallback(all_candidates, f"{type(exc).__name__}: {exc}"[:300])
    data = parse_json_object(response.text, fallback={})
    if "parse_error" in data:
        return _fallback(all_candidates, f"the answer wasn't valid JSON: {data['parse_error'][:200]}")

    by_id = {item.id: item for item in candidates}
    chosen: list[ProfileItem] = []
    for item_id in _parse_ids(data.get("item_ids")):
        if item_id in by_id and by_id[item_id] not in chosen:
            chosen.append(by_id[item_id])
    chosen = chosen[:MAX_EVIDENCE]
    if not chosen:
        return _fallback(all_candidates, "the answer named none of the offered record ids")

    # "reasons" list is the current shape; the id-keyed "why" object is what
    # the prompt asked for before structured outputs, still accepted.
    raw_why = data.get("why") if isinstance(data.get("why"), dict) else {}
    pairs = list(raw_why.items())
    for entry in data.get("reasons") if isinstance(data.get("reasons"), list) else []:
        if isinstance(entry, dict):
            pairs.append((entry.get("item_id"), entry.get("why")))
    why = {}
    for key, reason in pairs:
        ids = _parse_ids([key])
        if ids and ids[0] in by_id and isinstance(reason, str):
            why[ids[0]] = reason.strip()
    return EvidenceSelection(items=_ensure_a_cv(chosen, all_candidates), why=why)


def items_by_ids(db: Session, ids: list[int], exclude_ids: list[int] | tuple = ()) -> list[ProfileItem]:
    """Reload a saved selection, in its saved order, skipping records deleted
    or excluded since."""
    wanted = [i for i in ids if i not in set(exclude_ids)]
    found = {item.id: item for item in candidate_items(db) if item.id in wanted}
    return [found[i] for i in wanted if i in found]


# Claims a model-written framing line may not make unless a fact backs it.
RISKY_CLAIM_RE = re.compile(
    r"\b(led|lead|managed|mentored|production|deployed|users?|customers?|clients?|scaled|million|team of|"
    r"liderlik|yönettim|yönetti|üretimde|kullanıcı|müşteri|ekibe|ekibi)\b",
    re.IGNORECASE,
)
DESCRIPTION_FIELDS = ("one_liner", "problem", "approach", "architecture")


def citable_text(item: ProfileItem) -> str:
    """The parts of a record a letter may cite as fact: what code verified or
    computed, what the owner wrote, verbatim CV text, and the repo
    description. Model framing (role angles, talking points, the model's guess
    at the owner's contribution) is not citable."""
    extracted = item.extracted or {}
    owner_fields = set(item.owner_edited_fields or [])
    parts = [json.dumps(item.facts or {}, ensure_ascii=False)]
    parts += [f"{r.get('claim', '')} {r.get('quote', '')}" for r in extracted.get("results") or []]
    parts += [f"{u.get('tech', '')} {u.get('for', '')}" for u in extracted.get("stack_used_for") or []]
    parts += [str(extracted.get(field, "")) for field in DESCRIPTION_FIELDS]
    parts += [fact.get("answer", "") for fact in item.owner_facts or []]
    if "entry_type" in extracted:  # CV / LinkedIn entries are verbatim
        parts += [extracted.get(k, "") for k in ("title", "organization", "start", "end", "location")] + list(extracted.get("bullets") or [])
    elif "one_liner" not in extracted:  # v1 records: the four fields are all there is
        parts.append(json.dumps(extracted, ensure_ascii=False))
    parts += [json.dumps(extracted.get(field), ensure_ascii=False) for field in owner_fields]
    return "\n".join(p for p in parts if p)


def _framing_ok(text: str, facts_text: str) -> bool:
    """A model-written framing line enters a prompt only when its numbers and
    technologies are in the facts and it makes no unbacked risky claim."""
    from app.applications.grounding import grounding_misses

    misses = grounding_misses(text, facts_text)
    if misses["numbers"] or misses["technologies"]:
        return False
    for match in RISKY_CLAIM_RE.finditer(text):
        if match.group(0).casefold() not in facts_text.casefold():
            return False
    return True


def _commit_fact(item: ProfileItem) -> str:
    owner = (((item.facts or {}).get("activity") or {}).get("owner_commits")) or {}
    if not owner.get("scanned"):
        return ""
    return f"COMMITS (code): the owner authored {owner['by_owner']} of the {owner['scanned']} most recent commits"


def _rich_block(item: ProfileItem, role_type: str) -> str:
    extracted = item.extracted or {}
    owner_fields = set(item.owner_edited_fields or [])
    facts_text = citable_text(item)
    lines = ["FACTS (citable):"]
    for key in DESCRIPTION_FIELDS:
        value = extracted.get(key)
        if value:
            value = "; ".join(value) if isinstance(value, list) else value
            lines.append(f"{key.upper()}: {value}")
    uses = "; ".join(f"{u.get('tech')} - {u.get('for')}" for u in extracted.get("stack_used_for") or [])
    if uses:
        lines.append(f"STACK USED FOR: {uses}")
    for result in extracted.get("results") or []:
        lines.append(f'RESULT: {result.get("claim")} (quote: "{result.get("quote")}")')
    maturity = extracted.get("maturity") or {}
    flags = [key for key in ("tests", "ci", "deployed", "packaged") if maturity.get(key)]
    if flags:
        lines.append(f"MATURITY (code): {', '.join(flags)}")
    commit = _commit_fact(item)
    if commit:
        lines.append(commit)
    if "owner_contribution" in owner_fields and extracted.get("owner_contribution"):
        lines.append(f"OWNER'S CONTRIBUTION (written by the owner): {extracted['owner_contribution']}")

    framing = []
    angle = (extracted.get("role_angles") or {}).get(role_type, "")
    if angle and ("role_angles" in owner_fields or _framing_ok(angle, facts_text)):
        framing.append(f"ANGLE FOR THIS ROLE ({role_type}): {angle}")
    for point in extracted.get("talking_points") or []:
        if "talking_points" in owner_fields or _framing_ok(point, facts_text):
            framing.append(f"TALKING POINT: {point}")
    if framing:
        lines.append("MODEL FRAMING (emphasis only - NOT a source; state nothing from it that the FACTS above don't say):")
        lines += framing
    return "\n".join(lines)


def _entry_block(item: ProfileItem) -> str:
    extracted = item.extracted or {}
    dates = " - ".join(part for part in (extracted.get("start"), extracted.get("end")) if part)
    lines = [f"{extracted.get('entry_type', '').upper()}: {extracted.get('role', '')}" + (f" ({dates})" if dates else "")]
    lines += [f"- {bullet}" for bullet in extracted.get("bullets") or []]
    return "\n".join(lines)


def evidence_prompt_section(items: list[ProfileItem], role_type: str = "ml_ai") -> str:
    """Records as the drafting prompt reads them: the role-relevant angle,
    verified results and talking points for rich repo records, verbatim
    bullets for CV entries, owner-stated facts for all - not a JSON dump."""
    blocks = []
    for item in items:
        extracted = item.extracted or {}
        if "one_liner" in extracted:
            body = _rich_block(item, role_type)
        elif "entry_type" in extracted:
            body = _entry_block(item)
        else:
            body = json.dumps({k: v for k, v in extracted.items() if k != "parse_error"}, ensure_ascii=False)
        stack = ", ".join(record_stack(item))
        if stack and "one_liner" in extracted:
            body += f"\nSTACK (from code): {stack}"
        for fact in item.owner_facts or []:
            body += f"\nOWNER STATED: {fact.get('question') + ' -> ' if fact.get('question') else ''}{fact.get('answer')}"
        urls = ", ".join(item.source_urls or []) or "none"
        blocks.append(f"[E{item.id}] {item.source_type} | {item.source_ref} | urls: {urls}\n{body[:MAX_RECORD_CHARS]}")
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
            "purpose": record_title(item)[:MAX_PURPOSE_CHARS],
            "why": why.get(item.id, ""),
            "used": item.id in used_ids,
        }
        for item in selection_items
    ]
