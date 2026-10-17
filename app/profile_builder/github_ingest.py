"""GitHub ingestion (CLAUDE.md section 3): list ALL repos deterministically via the
official API, pre-filter forks/empty repos as a suggestion only, the owner confirms
include/exclude, then one deep pass per included repo (README, topics, languages,
stars, recent commits). Per the section-3 depth caveat, a repo with no README
falls back to sampling actual source files instead of yielding a thin record."""
import re

from github import Github
from sqlalchemy.orm import Session

from app.llm.base import LLMProvider
from app.models import ProfileItem
from app.profile_builder.coverage import record_item
from app.profile_builder.extraction import EXTRACTION_FALLBACK, extract_item

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", "vendor", ".idea"}
_CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rb", ".java", ".c", ".cpp",
    ".rs", ".php", ".swift", ".kt", ".cs",
}
_ENTRY_POINT_NAMES = {"main.py", "app.py", "index.js", "index.ts", "server.py", "cli.py", "__init__.py"}
_MAX_SAMPLED_FILES = 4
_MAX_CHARS_PER_FILE = 2000
_MAX_DIRS_EXPLORED = 5
# A README with at least this many words of actual prose is enough on its own;
# below it (a title and a one-liner, a template stub) the code is read as well.
DETAILED_README_MIN_WORDS = 150

_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def readme_prose_words(readme_text: str) -> int:
    """Words a reader would actually learn from: code blocks (install
    commands), badges/images, link targets and HTML are not counted."""
    text = _FENCED_CODE_RE.sub(" ", readme_text)
    text = _IMAGE_RE.sub(" ", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _HTML_TAG_RE.sub(" ", text)
    return len(re.findall(r"[^\W_]{2,}", text))


def _sample_source_files(repo) -> str:
    """No README to lean on - walk a bounded slice of the file tree (root plus
    a handful of subdirectories) and pull actual source files instead. Bounded
    so one bare repo can't blow up the GitHub API call budget for the batch."""
    try:
        queue = list(repo.get_contents(""))
    except Exception:
        return ""

    candidates = []
    dirs_explored = 0
    while queue and dirs_explored < _MAX_DIRS_EXPLORED and len(candidates) < 50:
        entry = queue.pop(0)
        if entry.type == "dir":
            if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                continue
            dirs_explored += 1
            try:
                queue.extend(repo.get_contents(entry.path))
            except Exception:
                continue
        elif entry.type == "file":
            suffix = "." + entry.name.rsplit(".", 1)[-1] if "." in entry.name else ""
            if suffix in _CODE_EXTENSIONS:
                candidates.append(entry)

    candidates.sort(key=lambda e: 0 if e.name.lower() in _ENTRY_POINT_NAMES else 1)

    parts = []
    for entry in candidates[:_MAX_SAMPLED_FILES]:
        try:
            file_obj = repo.get_contents(entry.path)
            text = file_obj.decoded_content.decode("utf-8", errors="ignore")[:_MAX_CHARS_PER_FILE]
            parts.append(f"--- {entry.path} ---\n{text}")
        except Exception:
            continue

    return "\n\n".join(parts)


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
                "suggested_include": not repo.fork and repo.size > 0,
            }
        )
    return repos


def _repo_deep_context(gh: Github, full_name: str) -> str:
    repo = gh.get_repo(full_name)
    parts = [f"Repo: {repo.full_name}", f"Description: {repo.description or 'unknown'}"]

    try:
        topics = repo.get_topics()
        parts.append(f"Topics: {', '.join(topics) or 'unknown'}")
    except Exception:
        parts.append("Topics: unknown")

    try:
        langs = repo.get_languages()
        parts.append(f"Languages: {', '.join(langs.keys()) or 'unknown'}")
    except Exception:
        parts.append("Languages: unknown")

    parts.append(f"Stars: {repo.stargazers_count}")

    readme_text = ""
    try:
        readme = repo.get_readme()
        readme_text = readme.decoded_content.decode("utf-8", errors="ignore")
    except Exception:
        pass

    # A detailed README is the best summary of a project - read the code only
    # when the README can't carry the record on its own.
    if readme_text.strip():
        parts.append("README:\n" + readme_text[:8000])
        if readme_prose_words(readme_text) < DETAILED_README_MIN_WORDS:
            sampled = _sample_source_files(repo)
            if sampled:
                parts.append("README is brief - sampled source files to fill in:\n" + sampled)
    else:
        parts.append("README: unknown (none found)")
        sampled = _sample_source_files(repo)
        if sampled:
            parts.append("No README - sampled source files instead:\n" + sampled)

    try:
        commits = list(repo.get_commits()[:5])
        parts.append(
            "Recent commits:\n" + "\n".join(f"- {c.commit.message.splitlines()[0]}" for c in commits)
        )
    except Exception:
        parts.append("Recent commits: unknown")

    return "\n\n".join(parts)


def ingest_repo(db: Session, llm: LLMProvider, token: str, full_name: str) -> ProfileItem:
    repo_url = f"https://github.com/{full_name}"
    # A single bad repo (renamed/deleted between listing and ingest, network
    # hiccup) must never crash the whole batch - see the step-by-step ingest
    # loop in app/web/routes/profile.py.
    try:
        gh = Github(token) if token else Github()
        context = _repo_deep_context(gh, full_name)
        extracted = extract_item(llm, context)
    except Exception as exc:  # noqa: BLE001 - recorded as needs_review, not swallowed
        extracted = {**EXTRACTION_FALLBACK, "parse_error": str(exc)}

    return record_item(
        db, source_type="github_repo", source_ref=full_name, extracted=extracted, source_urls=[repo_url]
    )
