"""Change detection and re-analysis for repo records (roadmap phase 1, item 5).

"Check for changes" makes NO LLM call: it lists the owner's repos and compares
each one's pushed_at (and, only when that moved, its HEAD SHA) with the
`source_version` the record was built from. An unchanged repo is never sent
to a model again.

Re-analysis of a changed repo sends the previous record plus what changed
since its HEAD SHA (GitHub compare API) and gets an updated record back. It
is not saved over the old one: every field that differs waits in
`pending_update` as a diff for the owner, owner-edited fields stay his, and
owner facts are untouched. Re-synthesis is offered afterwards, never run.
"""
from github import Github
from sqlalchemy.orm import Session

from app.llm.base import LLMProvider
from app.llm.call_log import llm_context, set_llm_ids
from app.models import ProfileItem, ProfileItemStatus
from app.profile_builder.github_ingest import build_record, list_candidate_repos, owner_identity, source_version_of
from app.profile_builder.records import RICH_FIELDS
from app.profile_builder.repo_record import with_v1_fields
from app.profile_builder.repo_snapshot import head_sha_of, load_changes_since, load_repo_snapshot

MAX_CHANGED_FILES_READ = 12
# Fields compared for the diff; v1 mirrors are derived and follow the record.
DERIVED_FIELDS = ("purpose", "stack", "role", "highlights")


def check_for_changes(db: Session, token: str, gh: Github | None = None, repos: list[dict] | None = None) -> dict:
    """{"changed": [...], "new": [...], "deleted": [...], "unchanged": int}.
    Each changed entry: {full_name, item_id, reason, old_sha, new_sha}.
    `gh` / `repos` are injectable for tests; by default both come from the token."""
    gh = gh or Github(token)
    repos = {r["full_name"]: r for r in (repos if repos is not None else list_candidate_repos(token))}
    items = {
        item.source_ref: item
        for item in db.query(ProfileItem).filter(ProfileItem.source_type == "github_repo").all()
    }
    changed, new, unchanged = [], [], 0
    for full_name, repo in repos.items():
        item = items.get(full_name)
        if item is None:
            if repo["suggested_include"]:
                new.append({"full_name": full_name, "description": repo["description"]})
            continue
        version = item.source_version or {}
        if not version.get("head_sha"):
            changed.append({"full_name": full_name, "item_id": item.id, "reason": "never_versioned", "old_sha": "", "new_sha": ""})
            continue
        if version.get("pushed_at") == repo["pushed_at"]:
            unchanged += 1
            continue
        new_sha = head_sha_of(gh.get_repo(full_name))
        if new_sha and new_sha == version["head_sha"]:
            unchanged += 1  # a push to another branch
            continue
        changed.append({"full_name": full_name, "item_id": item.id, "reason": "head_moved", "old_sha": version["head_sha"], "new_sha": new_sha})
    deleted = [
        {"full_name": name, "item_id": item.id}
        for name, item in items.items()
        if name not in repos and item.status != ProfileItemStatus.SKIPPED
    ]
    return {"changed": changed, "new": new, "deleted": deleted, "unchanged": unchanged}


def _changes_text(snapshot, changes: dict) -> str:
    lines = []
    if changes.get("error"):
        lines.append(f"(compare failed: {changes['error'][:200]} - read the current files instead)")
    if changes.get("commits"):
        lines.append("COMMITS:\n" + "\n".join(f"- {message}" for message in changes["commits"]))
    if changes.get("files"):
        lines.append("CHANGED FILES:\n" + "\n".join(f"- {f['status']} {f['path']} ({f['changes']} lines)" for f in changes["files"]))
    return "\n\n".join(lines)


@llm_context("repo_reanalysis")
def reanalyze_repo(db: Session, llm: LLMProvider, draft_llm: LLMProvider, token: str, item: ProfileItem, gh: Github | None = None) -> ProfileItem:
    """Updated record proposed as a diff (pending_update); facts and
    source_version move to the new HEAD right away - they are code, not
    claims."""
    set_llm_ids(profile_item_id=item.id)
    gh = gh or Github(token)
    login, emails = owner_identity(gh)
    snapshot = load_repo_snapshot(gh, item.source_ref, login, emails)
    old_sha = (item.source_version or {}).get("head_sha", "")
    changes = load_changes_since(gh, item.source_ref, old_sha, snapshot.head_sha) if old_sha else {"commits": [], "files": []}
    previous = item.extracted if "one_liner" in (item.extracted or {}) else None
    record, facts = build_record(snapshot, llm, draft_llm, item.owner_facts or [], previous=previous, changes=_changes_text(snapshot, changes))

    if previous is None or item.status != ProfileItemStatus.ANALYZED:
        # A v1 or failed record has nothing worth diffing against - replace it
        # (owner-edited fields still win, via merge in record_item semantics).
        owner_fields = set(item.owner_edited_fields or [])
        old = item.extracted or {}
        item.extracted = {**record, **{k: old[k] for k in owner_fields if k in old}}
        item.status = ProfileItemStatus.ANALYZED
    else:
        # Fields the owner reviews go to the diff; code-derived ones (maturity,
        # the v1 mirrors) follow the new facts directly.
        reviewable = {name for name, _ in RICH_FIELDS}
        pending = dict(item.pending_update or {})
        extracted = dict(previous)
        for field, value in record.items():
            if field in reviewable:
                if value != previous.get(field):
                    pending[field] = value
            elif field not in DERIVED_FIELDS:
                extracted[field] = value
        item.extracted = with_v1_fields(extracted, facts)
        item.pending_update = pending
    item.facts = facts
    item.source_version = source_version_of(snapshot)
    db.commit()
    db.refresh(item)
    return item
