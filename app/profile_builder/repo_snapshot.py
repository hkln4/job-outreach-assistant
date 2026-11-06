"""Everything the profile builder reads from one GitHub repo, loaded once.

`RepoSnapshot` is plain data plus a `read_file` callable, so the facts,
code-reading plan and record builder (repo_facts.py, repo_reading.py,
repo_record.py) are pure functions over it and are tested without GitHub.
`load_repo_snapshot` is the only place that talks to PyGithub.
"""
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

MAX_COMMITS_SCANNED = 300
MAX_TREE_ENTRIES = 5000


@dataclass
class RepoSnapshot:
    full_name: str
    html_url: str = ""
    description: str = ""
    topics: list[str] = field(default_factory=list)
    languages: dict[str, int] = field(default_factory=dict)  # bytes per language
    stars: int = 0
    forks: int = 0
    homepage: str = ""
    license: str = ""
    pushed_at: str = ""
    default_branch: str = ""
    head_sha: str = ""
    # [{path, size}] for every blob in the tree (vendored paths included -
    # filtering is the reading planner's job).
    tree: list[dict] = field(default_factory=list)
    readme: str = ""
    readme_path: str = ""
    # newest first: [{sha, message, author_login, author_email, date}]
    commits: list[dict] = field(default_factory=list)
    total_commits: int = 0
    contributors: int = 0
    releases: int = 0
    tags: int = 0
    owner_login: str = ""
    owner_emails: list[str] = field(default_factory=list)
    read_file: Callable[[str], str] = field(default=lambda path: "", repr=False)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def head_sha_of(repo) -> str:
    try:
        return repo.get_branch(repo.default_branch).commit.sha
    except Exception:  # noqa: BLE001 - an empty repo has no branch
        return ""


def load_repo_snapshot(gh, full_name: str, owner_login: str = "", owner_emails: list[str] | None = None) -> RepoSnapshot:
    repo = gh.get_repo(full_name)
    snapshot = RepoSnapshot(
        full_name=repo.full_name,
        html_url=repo.html_url or f"https://github.com/{full_name}",
        description=repo.description or "",
        stars=repo.stargazers_count or 0,
        forks=getattr(repo, "forks_count", 0) or 0,
        homepage=getattr(repo, "homepage", "") or "",
        pushed_at=_iso(getattr(repo, "pushed_at", None)),
        default_branch=getattr(repo, "default_branch", "") or "",
        owner_login=owner_login,
        owner_emails=[e.lower() for e in owner_emails or []],
    )
    try:
        snapshot.topics = list(repo.get_topics())
    except Exception:  # noqa: BLE001
        pass
    try:
        snapshot.languages = dict(repo.get_languages())
    except Exception:  # noqa: BLE001
        pass
    try:
        license_info = getattr(repo, "license", None)
        snapshot.license = (getattr(license_info, "spdx_id", "") or getattr(license_info, "name", "")) if license_info else ""
    except Exception:  # noqa: BLE001
        pass

    snapshot.head_sha = head_sha_of(repo)
    if snapshot.head_sha:
        try:
            tree = repo.get_git_tree(snapshot.head_sha, recursive=True).tree
            snapshot.tree = [
                {"path": e.path, "size": getattr(e, "size", 0) or 0} for e in tree[:MAX_TREE_ENTRIES] if e.type == "blob"
            ]
        except Exception:  # noqa: BLE001
            pass

    try:
        readme = repo.get_readme()
        snapshot.readme = readme.decoded_content.decode("utf-8", errors="ignore")
        snapshot.readme_path = readme.path
    except Exception:  # noqa: BLE001
        pass

    try:
        commits = repo.get_commits()
        snapshot.total_commits = commits.totalCount
        for commit in commits[:MAX_COMMITS_SCANNED]:
            author = commit.author
            snapshot.commits.append(
                {
                    "sha": commit.sha,
                    "message": (commit.commit.message or "").splitlines()[0][:200],
                    "author_login": getattr(author, "login", "") if author else "",
                    "author_email": (commit.commit.author.email or "").lower() if commit.commit.author else "",
                    "date": _iso(commit.commit.author.date) if commit.commit.author else "",
                }
            )
    except Exception:  # noqa: BLE001 - an empty repo has no commits
        pass
    for attr, getter in (("contributors", "get_contributors"), ("releases", "get_releases"), ("tags", "get_tags")):
        try:
            setattr(snapshot, attr, getattr(repo, getter)().totalCount)
        except Exception:  # noqa: BLE001
            pass

    def read_file(path: str) -> str:
        try:
            return repo.get_contents(path, ref=snapshot.head_sha or None).decoded_content.decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            return ""

    snapshot.read_file = read_file
    return snapshot


def load_changes_since(gh, full_name: str, base_sha: str, head_sha: str) -> dict:
    """Commit messages and changed files between two SHAs (GitHub compare API)."""
    try:
        comparison = gh.get_repo(full_name).compare(base_sha, head_sha)
    except Exception as exc:  # noqa: BLE001 - force-push / deleted base: re-analysis still works without it
        return {"error": str(exc), "commits": [], "files": []}
    return {
        "commits": [(c.commit.message or "").splitlines()[0][:200] for c in comparison.commits][:100],
        "files": [{"path": f.filename, "status": f.status, "changes": f.changes} for f in comparison.files][:200],
    }
