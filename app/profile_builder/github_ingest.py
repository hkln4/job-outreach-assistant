"""GitHub ingestion (CLAUDE.md section 3, roadmap phase 1): list ALL repos
deterministically via the official API, pre-filter forks/empty repos as a
suggestion only, the owner confirms include/exclude, then one deep pass per
included repo:

1. load a RepoSnapshot (tree, README, commits, manifests on demand),
2. plan and read the code (app/profile_builder/repo_reading.py - the model
   only ranks paths),
3. compute deterministic facts (repo_facts.py - stack, tests, CI, commit
   share), the record's stack comes from here,
4. the draft model writes the rich record, code verifies it (repo_record.py),
5. store it with `facts` and `source_version` so "check for changes" can
   skip unchanged repos without any LLM call.
"""
from datetime import datetime, timezone

from github import Github
from sqlalchemy.orm import Session

from app.llm.base import LLMProvider
from app.models import ProfileItem
from app.profile_builder.coverage import record_item
from app.profile_builder.extraction import EXTRACTION_FALLBACK
from app.profile_builder.repo_facts import compute_facts
from app.profile_builder.repo_reading import plan_and_read
from app.profile_builder.repo_record import verify_record, write_record
from app.profile_builder.repo_snapshot import RepoSnapshot, load_repo_snapshot


def list_candidate_repos(token: str) -> list[dict]:
    """Every repo the token's owner can see (public AND private) - nothing is
    dropped here, only annotated with a suggested_include flag. The owner sees
    all of them and confirms.

    Uses the token's own identity (GET /user) rather than a username string:
    Github(token).get_user(some_name) fetches that *public* profile regardless
    of who the token belongs to, silently returning zero or the wrong repos on
    a typo, and only ever sees that account's public repos even when correct."""
    if not token:
        raise ValueError("GITHUB_TOKEN is not set in .env.")
    gh = Github(token)
    me = gh.get_user()
    repos = []
    for repo in me.get_repos():
        repos.append(
            {
                "full_name": repo.full_name,
                "description": repo.description or "",
                "is_fork": repo.fork,
                "is_empty": repo.size == 0,
                "is_private": repo.private,
                "stars": repo.stargazers_count,
                "language": repo.language or "",
                "html_url": repo.html_url,
                "pushed_at": repo.pushed_at.isoformat() if repo.pushed_at else "",
                "default_branch": repo.default_branch or "",
                "suggested_include": not repo.fork and repo.size > 0,
            }
        )
    return repos


def owner_identity(gh: Github) -> tuple[str, list[str]]:
    """The token owner's login and the emails his commits are likely signed
    with, for the commit-share fact. No /user/emails call: a fine-grained
    token without the email permission gets a 403 there (logged by PyGithub
    on every repo), and the login already matches nearly every commit. The
    public profile email and GitHub's noreply addresses cover the rest."""
    try:
        me = gh.get_user()
        emails = [f"{me.login}@users.noreply.github.com".lower()]
        if getattr(me, "id", None):
            emails.append(f"{me.id}+{me.login}@users.noreply.github.com".lower())
        if getattr(me, "email", None):
            emails.append(me.email.lower())
        return me.login, emails
    except Exception:  # noqa: BLE001 - without it the commit share is just unknown
        return "", []


def source_version_of(snapshot: RepoSnapshot) -> dict:
    return {
        "pushed_at": snapshot.pushed_at,
        "head_sha": snapshot.head_sha,
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
    }


def build_record(
    snapshot: RepoSnapshot,
    llm: LLMProvider,
    draft_llm: LLMProvider,
    owner_facts: list[dict] | None = None,
    previous: dict | None = None,
    changes: str = "",
) -> tuple[dict, dict]:
    """(verified record, facts) for one snapshot."""
    plan = plan_and_read(snapshot, llm)
    facts = compute_facts(snapshot, plan.files)
    record = write_record(
        draft_llm,
        full_name=snapshot.full_name,
        description=snapshot.description,
        topics=snapshot.topics,
        facts=facts,
        material=plan.prompt_section(),
        owner_facts=owner_facts or [],
        previous=previous,
        changes=changes,
    )
    verified = verify_record(
        record,
        quotable=plan.quotable_text(),
        owner_facts=owner_facts or [],
        facts=facts,
        readme=snapshot.readme,
        previous=previous,
    )
    facts["reading"] = {
        "files_read": list(plan.files),
        "modules_summarized": list(plan.module_summaries),
        "code_budget": plan.code_budget,
        "large_repo": plan.large_repo,
        "ranked_paths_dropped": plan.dropped_ranked,
    }
    return verified, facts


def ingest_repo(
    db: Session,
    llm: LLMProvider,
    token: str,
    full_name: str,
    draft_llm: LLMProvider | None = None,
    owner: tuple[str, list[str]] | None = None,
) -> ProfileItem:
    """First analysis of a repo (or a forced full one). `llm` ranks paths and
    summarizes modules; `draft_llm` writes the record (falls back to `llm`)."""
    repo_url = f"https://github.com/{full_name}"
    # A single bad repo (renamed/deleted between listing and ingest, network
    # hiccup) must never crash the whole batch - see the step-by-step ingest
    # loop in app/web/routes/profile.py.
    existing = (
        db.query(ProfileItem)
        .filter(ProfileItem.source_type == "github_repo", ProfileItem.source_ref == full_name)
        .order_by(ProfileItem.id.desc())
        .first()
    )
    facts = None
    version = None
    try:
        gh = Github(token) if token else Github()
        login, emails = owner if owner is not None else owner_identity(gh)
        snapshot = load_repo_snapshot(gh, full_name, login, emails)
        extracted, facts = build_record(snapshot, llm, draft_llm or llm, existing.owner_facts if existing else [])
        version = source_version_of(snapshot)
        repo_url = snapshot.html_url or repo_url
    except Exception as exc:  # noqa: BLE001 - recorded as needs_review, not swallowed
        extracted = {**EXTRACTION_FALLBACK, "parse_error": str(exc)}

    return record_item(
        db,
        source_type="github_repo",
        source_ref=full_name,
        extracted=extracted,
        source_urls=[repo_url],
        facts=facts,
        source_version=version,
    )
