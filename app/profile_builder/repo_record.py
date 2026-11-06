"""The rich per-repo profile record (roadmap phase 1, item 3).

Written once per repo by the DRAFT model (every later letter and CV depends
on it), with native structured output, then checked by code:
- every results[].quote must be found verbatim in the text that was sent
  (README + raw code, or an owner fact for source "owner"); unmatched results
  move to claims_to_verify;
- a technology in stack_used_for that the manifests, imports or README don't
  back moves to claims_to_verify;
- maturity tests/ci/deployed/packaged come from the deterministic facts, not
  from the model (only its notes are kept);
- open_questions are capped at 5.
The v1 fields (purpose, stack, role, highlights) are derived from the rich
record so older readers keep working.
"""
import json
import re
from typing import Literal

from pydantic import BaseModel, Field

from app.llm.base import LLMProvider
from app.llm.call_log import llm_context
from app.llm.json_utils import parse_json_object
from app.profile_builder.repo_facts import allowed_technologies, technology_is_backed

MAX_OPEN_QUESTIONS = 5
MIN_QUOTE_CHARS = 4
ROLE_ANGLES = ("ml_ai", "computer_vision", "backend_data", "mlops")

RECORD_SYSTEM_PROMPT = """You are writing the profile record for ONE of the owner's software \
projects (a GitHub repository). The owner is a new graduate; this record is the only thing later \
cover letters and CVs may say about the project, so it must be accurate, specific and defensible \
in an interview.

You get: deterministic FACTS computed by code (stack from manifests/imports, tests, CI, \
deployment files, commit stats including the owner's share of commits), the README, the most \
informative source files, possibly model-written module summaries (context only), and any facts \
the owner stated himself (OWNER FACTS).

Rules:
- Base everything on the given material. Never invent features, users, metrics or results.
- one_liner: what it is, in plain language (<= 20 words). problem: why it exists. approach: \
how it works. architecture: main components / data flow, one per item.
- stack_used_for: what each technology was used FOR in this project ("PyTorch" -> "U-Net \
training"). Only technologies listed in ALLOWED TECHNOLOGIES or named in the README.
- owner_contribution: what the owner did, from the commit facts and OWNER FACTS. If they don't \
say, write "unknown" - do not assume solo work from commit counts alone unless the share is 100%.
- results: concrete outcomes (metrics, scale, users, benchmarks). Each needs "quote": a short \
span copied EXACTLY, character for character, from the README, a source file or an owner fact, \
and "source" saying which (readme | code | owner). No quote, no result - put it in \
claims_to_verify instead.
- maturity.notes: one sentence on how finished/robust it is (tests, CI, deployment are already \
known from facts).
- domains: e.g. "medical imaging", "ETL", "NLP", "computer vision".
- role_angles: for each of ml_ai, computer_vision, backend_data, mlops, one or two sentences \
framing this project for that kind of role, truthfully - "not relevant" when it isn't. These \
must pass an interview: emphasis is fine, claiming work not done is not.
- talking_points: interview-defensible one-liners about decisions, trade-offs, difficulties.
- claims_to_verify: README claims the code/facts don't back, and anything uncertain.
- open_questions: at most 5 questions only the owner can answer that would most improve this \
record (solo or team? final metric? who used it? why built?). Skip what OWNER FACTS answer.
- Write in English.
"""

REANALYSIS_ADDENDUM = """
This is a RE-ANALYSIS. You also get the PREVIOUS RECORD and the CHANGES since it was written \
(commit messages, changed files and their current content). Return the full updated record: \
keep what is still true, update what changed, add what is new. Results from the previous \
record may be kept with their quotes.
"""


class _StackUse(BaseModel):
    tech: str
    for_: str = Field(alias="for")

    model_config = {"populate_by_name": True}


class _Result(BaseModel):
    claim: str
    quote: str
    source: Literal["readme", "code", "owner"]


class _Maturity(BaseModel):
    tests: bool = False
    ci: bool = False
    deployed: bool = False
    packaged: bool = False
    notes: str = ""


class _RoleAngles(BaseModel):
    ml_ai: str = ""
    computer_vision: str = ""
    backend_data: str = ""
    mlops: str = ""


class RepoRecord(BaseModel):
    one_liner: str
    problem: str
    approach: str
    architecture: list[str]
    stack_used_for: list[_StackUse]
    owner_contribution: str
    results: list[_Result]
    maturity: _Maturity
    domains: list[str]
    role_angles: _RoleAngles
    talking_points: list[str]
    claims_to_verify: list[str]
    open_questions: list[str]


RECORD_FALLBACK = {"one_liner": "unknown"}


def _normalize(text: str) -> str:
    text = (text or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip().casefold()


def quote_found(quote: str, haystack_normalized: str) -> bool:
    needle = _normalize(quote).strip("\"'`")
    return len(needle) >= MIN_QUOTE_CHARS and needle in haystack_normalized


def facts_summary(facts: dict) -> str:
    """The deterministic facts as compact prompt text."""
    activity = facts.get("activity", {})
    owner = activity.get("owner_commits", {})
    share = owner.get("share")
    lines = [
        f"STACK (from manifests, imports, languages, tooling): {', '.join(facts.get('stack', [])) or 'none found'}",
        f"DEPENDENCIES: {', '.join(facts.get('dependencies', [])[:60]) or 'none'}",
        f"LANGUAGES (bytes): {json.dumps(facts.get('languages_bytes', {}))}",
        f"TOP-LEVEL DIRS: {', '.join(facts.get('top_level_dirs', [])) or 'none'}",
        f"TESTS: {'yes, ' + str(facts['tests']['count']) + ' test files' if facts.get('tests', {}).get('present') else 'none found'}",
        f"CI: {', '.join(facts.get('ci_workflows', [])) or 'none'}",
        f"DEPLOYMENT FILES: {', '.join(facts.get('deployment_files', [])) or 'none'}",
        f"NOTEBOOKS: {facts.get('notebooks', 0)}",
        f"LICENSE: {facts.get('license') or 'none'} | PACKAGED AS: {facts.get('packaged') or 'not packaged'} | HOMEPAGE: {facts.get('homepage') or 'none'}",
        f"COMMITS: {activity.get('total_commits', 0)} total, first {activity.get('first_commit', '')[:10]}, last {activity.get('last_commit', '')[:10]}; "
        f"owner authored {owner.get('by_owner', 0)} of the {owner.get('scanned', 0)} most recent"
        + (f" ({int(share * 100)}%)" if share is not None else "")
        + f"; contributors {activity.get('contributors', 0)}; releases {activity.get('releases', 0)}; stars {activity.get('stars', 0)}",
    ]
    if facts.get("claims_to_verify"):
        lines.append("README CLAIMS NOT BACKED BY FILES: " + " ".join(facts["claims_to_verify"]))
    return "\n".join(lines)


def owner_facts_text(owner_facts: list[dict]) -> str:
    return "\n".join(
        f"- {fact['question']} -> {fact['answer']}" if fact.get("question") else f"- {fact['answer']}"
        for fact in owner_facts or []
    )


def verify_record(record: dict, *, quotable: str, owner_facts: list[dict], facts: dict, readme: str, previous: dict | None = None) -> dict:
    """Apply the code-side rules to a model-written record (see module doc)."""
    record = json.loads(json.dumps(record))  # deep copy
    claims = list(record.get("claims_to_verify") or [])
    # README and code were both sent; owner facts are quotable too. A result
    # carried over from the previous record keeps its (already checked) quote.
    sent = _normalize(f"{readme}\n{quotable}")
    owner = _normalize(" ".join(f["answer"] for f in owner_facts or []))
    previous_quotes = {_normalize(r.get("quote", "")) for r in (previous or {}).get("results") or []}
    kept = []
    for result in record.get("results") or []:
        quote = result.get("quote", "")
        found = quote_found(quote, sent) or quote_found(quote, owner)
        if found or (quote and _normalize(quote) in previous_quotes):
            kept.append(result)
        else:
            claims.append(f"Unverified result (quote not found in the source): {result.get('claim', '')}")
    record["results"] = kept

    allowed = allowed_technologies(facts, readme)
    stack_used = []
    for use in record.get("stack_used_for") or []:
        tech = use.get("tech", "")
        if technology_is_backed(tech, allowed):
            stack_used.append(use)
        else:
            claims.append(f"Technology not found in manifests, imports or README: {tech}")
    record["stack_used_for"] = stack_used

    maturity = dict(record.get("maturity") or {})
    for key, value in (facts.get("maturity") or {}).items():
        maturity[key] = bool(value)
    record["maturity"] = maturity

    for claim in facts.get("claims_to_verify") or []:
        if claim not in claims:
            claims.append(claim)
    record["claims_to_verify"] = list(dict.fromkeys(claims))
    answered = {f.get("question") for f in owner_facts or []}
    record["open_questions"] = [q for q in record.get("open_questions") or [] if q not in answered][:MAX_OPEN_QUESTIONS]
    angles = record.get("role_angles") or {}
    record["role_angles"] = {key: angles.get(key, "") for key in ROLE_ANGLES}
    return with_v1_fields(record, facts)


def with_v1_fields(record: dict, facts: dict) -> dict:
    """purpose / stack / role / highlights for readers of the v1 schema."""
    record["purpose"] = " ".join(part for part in (record.get("one_liner"), record.get("problem")) if part and part != "unknown") or "unknown"
    record["stack"] = list(facts.get("stack") or [use.get("tech") for use in record.get("stack_used_for") or []])
    record["role"] = record.get("owner_contribution") or "unknown"
    record["highlights"] = [r.get("claim", "") for r in record.get("results") or []] + list(record.get("talking_points") or [])[:3]
    return record


@llm_context("repo_record")
def write_record(
    draft_llm: LLMProvider,
    *,
    full_name: str,
    description: str,
    topics: list[str],
    facts: dict,
    material: str,
    owner_facts: list[dict],
    previous: dict | None = None,
    changes: str = "",
) -> dict:
    """The model-written record, before verify_record. Raises on an
    unparseable answer (the caller records it as needs_review)."""
    allowed = sorted(set(facts.get("stack", [])))
    prompt = (
        f"REPOSITORY: {full_name}\nDESCRIPTION: {description or 'none'}\nTOPICS: {', '.join(topics) or 'none'}\n\n"
        f"FACTS (computed by code - authoritative):\n{facts_summary(facts)}\n\n"
        f"ALLOWED TECHNOLOGIES: {', '.join(allowed) or 'only what the README names'}\n\n"
        f"OWNER FACTS (stated by the owner - quotable with source \"owner\"):\n{owner_facts_text(owner_facts) or 'none'}\n\n"
        f"{material}"
    )
    system = RECORD_SYSTEM_PROMPT
    if previous is not None:
        prompt = (
            f"PREVIOUS RECORD:\n{json.dumps({k: v for k, v in previous.items() if k not in ('purpose', 'stack', 'role', 'highlights')}, ensure_ascii=False)}\n\n"
            f"CHANGES SINCE THE PREVIOUS RECORD:\n{changes or 'none listed'}\n\n" + prompt
        )
        system += REANALYSIS_ADDENDUM
    response = draft_llm.generate(
        prompt=prompt,
        system=system,
        temperature=0.0,
        max_output_tokens=4096,
        json_schema=RepoRecord.model_json_schema(by_alias=True),
    )
    data = parse_json_object(response.text, fallback=RECORD_FALLBACK)
    if "parse_error" in data:
        raise ValueError(f"The record answer wasn't valid JSON: {data['parse_error'][:200]}")
    try:
        return RepoRecord.model_validate(data).model_dump(by_alias=True)
    except ValueError as exc:
        raise ValueError(f"The record answer had the wrong shape: {str(exc)[:300]}") from exc
