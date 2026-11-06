"""Code reading the app plans, the model only ranks (roadmap phase 1, item 2).

The app builds the file tree (vendored/generated paths skipped), always reads
the entry points and manifests, and offers the reading model the remaining
PATHS ONLY to rank. The top files are read until PROFILE_REPO_CHAR_BUDGET is
spent. A detailed README lowers the code budget but never skips code. Large
repos are summarized per top-level module first (map) and the summaries go to
the record call (reduce) next to a smaller slice of raw code.

Everything that may be quoted as evidence (README + raw file text) is kept in
`quotable_text`; model-written module summaries are labeled and never
quotable.
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from pydantic import BaseModel

from app.config import get_settings
from app.llm.base import LLMProvider
from app.llm.call_log import llm_context
from app.llm.json_utils import parse_json_object
from app.profile_builder.repo_facts import MANIFEST_RE, VENDORED_RE
from app.profile_builder.repo_snapshot import RepoSnapshot

CODE_EXTENSIONS = {
    ".py", ".ipynb", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".c", ".cc", ".cpp", ".h",
    ".hpp", ".cs", ".rb", ".php", ".swift", ".scala", ".sql", ".sh", ".r", ".jl", ".m", ".dart", ".vue", ".svelte",
}
CONFIG_EXTENSIONS = {".yaml", ".yml", ".toml", ".cfg", ".ini", ".json"}
ENTRY_POINT_NAMES = {
    "main.py", "app.py", "train.py", "inference.py", "predict.py", "server.py", "cli.py", "__main__.py",
    "manage.py", "run.py", "pipeline.py", "index.js", "index.ts", "main.go", "main.rs", "main.ts", "app.ts",
    "server.js", "server.ts", "main.java", "Application.java",
}
MAX_ENTRY_POINTS = 6
MAX_PATHS_OFFERED = 400
MAX_RANKED = 15
MAX_FILE_BYTES = 200_000
MAX_CHARS_PER_FILE = 8000
MAX_README_CHARS = 10000
DETAILED_README_MIN_WORDS = 150
DETAILED_README_CODE_SHARE = 0.6
# Above these, summarize per top-level module first.
LARGE_REPO_CODE_FILES = 150
MAX_MODULES = 6
MODULE_SUMMARY_CHARS = 1500

RANK_SYSTEM_PROMPT = """You are choosing which files of a software repository are the most \
informative for understanding what the project does and how: core logic, models, pipelines, \
API handlers - not boilerplate, generated files, fixtures or tiny configs. You only see paths.
Return at most {k} paths, most informative first, copied exactly from the list. Output ONLY JSON:
{{"paths": [string]}}"""

MODULE_SYSTEM_PROMPT = """Summarize what this module of a repository does, from its code only: \
purpose, main components, data flow, notable techniques. Never invent - say "unclear" when the \
code doesn't show it. At most 8 short sentences, plain text."""


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


class _Ranked(BaseModel):
    paths: list[str]


@dataclass
class ReadingPlan:
    readme: str = ""
    files: dict[str, str] = field(default_factory=dict)  # path -> text read
    module_summaries: dict[str, str] = field(default_factory=dict)
    ranked_by_model: list[str] = field(default_factory=list)
    dropped_ranked: list[str] = field(default_factory=list)  # paths the model named that weren't offered
    code_budget: int = 0
    large_repo: bool = False

    def quotable_text(self) -> str:
        parts = [self.readme] + list(self.files.values())
        return "\n".join(part for part in parts if part)

    def prompt_section(self) -> str:
        parts = []
        if self.readme:
            parts.append(f"README:\n{self.readme}")
        else:
            parts.append("README: none")
        for path, text in self.files.items():
            parts.append(f"--- FILE {path} ---\n{text}")
        if self.module_summaries:
            parts.append(
                "MODULE SUMMARIES (written by a model from the code - context only, NOT quotable evidence):\n"
                + "\n".join(f"[{module}] {summary}" for module, summary in self.module_summaries.items())
            )
        return "\n\n".join(parts)


def _suffix(path: str) -> str:
    return PurePosixPath(path).suffix.lower()


def own_files(snapshot: RepoSnapshot) -> list[dict]:
    return [
        entry
        for entry in snapshot.tree
        if not VENDORED_RE.search(entry["path"]) and (entry.get("size") or 0) <= MAX_FILE_BYTES
    ]


def notebook_text(raw: str) -> str:
    """Cell sources and text outputs of a notebook (outputs often hold the
    real metrics), without the JSON noise."""
    try:
        notebook = json.loads(raw)
    except ValueError:
        return raw
    parts = []
    for cell in notebook.get("cells", []):
        source = "".join(cell.get("source") or [])
        if source.strip():
            parts.append(f"[{cell.get('cell_type', 'cell')}]\n{source}")
        for output in cell.get("outputs") or []:
            text = "".join(output.get("text") or output.get("data", {}).get("text/plain") or [])
            if text.strip():
                parts.append(f"[output]\n{text.strip()[:1500]}")
    return "\n".join(parts)


def _read(snapshot: RepoSnapshot, path: str, limit: int) -> str:
    raw = snapshot.read_file(path)
    text = notebook_text(raw) if path.endswith(".ipynb") else raw
    return text[:limit]


def _rank_paths(llm: LLMProvider, snapshot: RepoSnapshot, paths: list[str], k: int) -> tuple[list[str], list[str]]:
    offered = paths[:MAX_PATHS_OFFERED]
    if len(offered) <= k:
        return offered, []
    prompt = (
        f"REPOSITORY: {snapshot.full_name}\nDESCRIPTION: {snapshot.description or 'none'}\n\n"
        "PATHS:\n" + "\n".join(offered)
    )
    try:
        response = llm.generate(
            prompt=prompt,
            system=RANK_SYSTEM_PROMPT.format(k=k),
            temperature=0.0,
            max_output_tokens=800,
            json_schema=_Ranked.model_json_schema(),
        )
    except Exception:  # noqa: BLE001 - fall back to size order, the record still gets code
        return sorted(offered, key=lambda p: -next(e["size"] for e in snapshot.tree if e["path"] == p))[:k], []
    data = parse_json_object(response.text, fallback={"paths": []})
    offered_set = set(offered)
    ranked, dropped = [], []
    for path in data.get("paths") or []:
        if path in offered_set and path not in ranked:
            ranked.append(path)
        elif path not in offered_set:
            dropped.append(str(path))
    return ranked[:k], dropped


@llm_context("repo_reading")
def plan_and_read(snapshot: RepoSnapshot, llm: LLMProvider, budget: int | None = None) -> ReadingPlan:
    budget = budget or get_settings().profile_repo_char_budget
    plan = ReadingPlan(readme=(snapshot.readme or "")[:MAX_README_CHARS])
    detailed = readme_prose_words(snapshot.readme or "") >= DETAILED_README_MIN_WORDS
    plan.code_budget = int(budget * (DETAILED_README_CODE_SHARE if detailed else 1.0))

    files = own_files(snapshot)
    code = [e["path"] for e in files if _suffix(e["path"]) in CODE_EXTENSIONS]
    manifests = [e["path"] for e in files if MANIFEST_RE.search(e["path"]) and e["path"].count("/") <= 2]
    entry_points = sorted(
        (p for p in code if PurePosixPath(p).name in ENTRY_POINT_NAMES),
        key=lambda p: (p.count("/"), p),
    )[:MAX_ENTRY_POINTS]
    always = list(dict.fromkeys(manifests + entry_points))
    rest = [p for p in code if p not in always]
    plan.large_repo = len(code) > LARGE_REPO_CODE_FILES

    remaining = plan.code_budget
    per_file = min(MAX_CHARS_PER_FILE, max(1500, plan.code_budget // 6))

    def take(path: str, limit: int) -> None:
        nonlocal remaining
        if remaining <= 0 or path in plan.files:
            return
        text = _read(snapshot, path, min(limit, remaining))
        if text.strip():
            plan.files[path] = text
            remaining -= len(text)

    for path in always:
        take(path, per_file // 2 if path in manifests else per_file)

    ranked, plan.dropped_ranked = _rank_paths(llm, snapshot, rest, MAX_RANKED)
    plan.ranked_by_model = ranked

    if plan.large_repo:
        # Map: summarize the biggest top-level modules from their top files,
        # with half the remaining budget; raw ranked files get the other half.
        module_budget = remaining // 2
        remaining -= module_budget
        by_module: dict[str, list[str]] = {}
        for path in ranked + rest:
            module = path.split("/", 1)[0] if "/" in path else "(root)"
            by_module.setdefault(module, [])
            if path not in by_module[module]:
                by_module[module].append(path)
        sizes = {m: sum(e["size"] for e in files if (e["path"].split("/", 1)[0] if "/" in e["path"] else "(root)") == m) for m in by_module}
        modules = sorted(by_module, key=lambda m: -sizes[m])[:MAX_MODULES]
        share = module_budget // max(1, len(modules))
        for module in modules:
            texts, used = [], 0
            for path in by_module[module][:4]:
                text = _read(snapshot, path, max(500, share // 4))
                if text.strip():
                    texts.append(f"--- {path} ---\n{text}")
                    used += len(text)
                if used >= share:
                    break
            if texts:
                plan.module_summaries[module] = _summarize_module(llm, snapshot.full_name, module, "\n".join(texts))

    for path in ranked:
        take(path, per_file)
    return plan


@llm_context("repo_module_summary")
def _summarize_module(llm: LLMProvider, repo: str, module: str, code: str) -> str:
    try:
        response = llm.generate(
            prompt=f"REPOSITORY: {repo}\nMODULE: {module}\n\n{code}",
            system=MODULE_SYSTEM_PROMPT,
            temperature=0.0,
            max_output_tokens=600,
        )
    except Exception as exc:  # noqa: BLE001 - a missing summary is shown, not fatal
        return f"(summary failed: {type(exc).__name__})"
    return response.text.strip()[:MODULE_SUMMARY_CHARS]
