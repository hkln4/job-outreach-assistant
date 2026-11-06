"""Roadmap phase 1 "done when": a fixture repo with a manifest, tests and CI
yields a stack equal to the manifest-derived list, maturity.tests/ci true from
code, a fabricated result without a quote moved to claims_to_verify, and
"check for changes" on an unchanged repo makes zero LLM calls."""
import json

from app.db import SessionLocal, init_db
from app.models import ProfileItem, ProfileItemStatus
from app.profile_builder import github_ingest, repo_changes
from app.profile_builder.coverage import record_item
from app.profile_builder.repo_facts import compute_facts, parse_manifest
from app.profile_builder.repo_reading import notebook_text, plan_and_read
from app.profile_builder.repo_snapshot import RepoSnapshot
from tests.fakes import SequencedLLMProvider

README = """# LesionSeg

Segments skin lesions in dermoscopy images with a U-Net. On the ISIC 2018 validation split the
final model reaches a Dice score of 0.87. Includes a FastAPI service for inference and unit tests.
Deployed with Docker. """ + "More prose about the dataset, augmentations and training schedule. " * 20

FILES = {
    "requirements.txt": "torch==2.3\nalbumentations>=1.4  # aug\nfastapi\nuvicorn[standard]\nnumpy\n-r dev.txt\n",
    "src/train.py": "import torch\nimport cv2\nfrom albumentations import Compose\n\ndef train():\n    loss = dice_loss()\n",
    "src/api.py": "from fastapi import FastAPI\napp = FastAPI()\n",
    "tests/test_api.py": "def test_health():\n    assert True\n",
    "tests/test_train.py": "def test_loss():\n    assert True\n",
    ".github/workflows/ci.yml": "on: push\njobs: {}\n",
    "node_modules/left-pad/index.js": "module.exports = 1",
    "notebooks/eval.ipynb": json.dumps(
        {"cells": [{"cell_type": "code", "source": ["print(dice)"], "outputs": [{"text": ["Dice: 0.871\n"]}]}]}
    ),
}


def _snapshot(head_sha: str = "abc123", pushed_at: str = "2026-10-01T10:00:00+00:00") -> RepoSnapshot:
    return RepoSnapshot(
        full_name="owner/lesion-seg",
        html_url="https://github.com/owner/lesion-seg",
        description="Skin lesion segmentation",
        languages={"Python": 9000, "Jupyter Notebook": 4000, "Shell": 100},
        pushed_at=pushed_at,
        head_sha=head_sha,
        tree=[{"path": path, "size": len(text)} for path, text in FILES.items()],
        readme=README,
        commits=[
            {"sha": "1", "message": "train", "author_login": "owner", "author_email": "", "date": "2026-09-01T00:00:00"},
            {"sha": "2", "message": "init", "author_login": "friend", "author_email": "", "date": "2026-01-01T00:00:00"},
        ],
        total_commits=2,
        contributors=2,
        owner_login="owner",
        read_file=lambda path: FILES.get(path, ""),
    )


def _record_answer(**overrides) -> str:
    record = {
        "one_liner": "U-Net segmentation of skin lesions with a FastAPI inference service.",
        "problem": "Manual lesion outlining is slow.",
        "approach": "U-Net trained on ISIC 2018 with augmentations.",
        "architecture": ["training script", "FastAPI service"],
        "stack_used_for": [
            {"tech": "PyTorch", "for": "U-Net training"},
            {"tech": "Kubernetes", "for": "serving"},  # not in manifests/imports/README
        ],
        "owner_contribution": "unknown",
        "results": [
            {"claim": "Dice 0.87 on ISIC 2018 validation", "quote": "reaches a Dice score of 0.87", "source": "readme"},
            {"claim": "Used by 3 hospitals", "quote": "deployed in 3 hospitals", "source": "readme"},  # fabricated
        ],
        "maturity": {"tests": False, "ci": False, "deployed": False, "packaged": True, "notes": "Research prototype."},
        "domains": ["medical imaging", "computer vision"],
        "role_angles": {"ml_ai": "Trained a U-Net.", "computer_vision": "Segmentation.", "backend_data": "FastAPI service.", "mlops": "not relevant"},
        "talking_points": ["Chose Dice loss for class imbalance."],
        "claims_to_verify": [],
        "open_questions": ["Was this solo?", "Q2", "Q3", "Q4", "Q5", "Q6"],
    }
    record.update(overrides)
    return json.dumps(record)


def test_manifest_parsers_read_real_dependencies():
    assert parse_manifest("requirements.txt", FILES["requirements.txt"]) == ["torch", "albumentations", "fastapi", "uvicorn", "numpy"]
    pyproject = '[project]\nname = "lesionseg"\ndependencies = ["transformers>=4", "pandas"]\n[tool.poetry.dependencies]\npython = "^3.11"\nredis = "*"\n'
    assert parse_manifest("pyproject.toml", pyproject) == ["transformers", "pandas", "redis"]
    assert parse_manifest("package.json", '{"dependencies": {"react": "18"}, "devDependencies": {"vite": "5"}}') == ["react", "vite"]
    assert parse_manifest("go.mod", "module x\nrequire (\n\tgithub.com/gin-gonic/gin v1.9.1\n)\n") == ["github.com/gin-gonic/gin"]


def test_facts_come_from_code_and_ignore_vendored_paths():
    snapshot = _snapshot()
    facts = compute_facts(snapshot, {"src/train.py": FILES["src/train.py"]})
    # Python (>=5% of bytes), manifest deps mapped to display names, then imports, then CI tooling
    assert facts["stack"] == [
        "Python", "Jupyter Notebook", "PyTorch", "Albumentations", "FastAPI", "Uvicorn", "NumPy", "OpenCV", "GitHub Actions",
    ]
    assert facts["tests"] == {"present": True, "count": 2, "examples": ["tests/test_api.py", "tests/test_train.py"]}
    assert facts["maturity"]["tests"] and facts["maturity"]["ci"]
    assert not facts["maturity"]["deployed"]
    assert facts["activity"]["owner_commits"] == {"scanned": 2, "by_owner": 1, "share": 0.5}
    assert "node_modules" not in facts["top_level_dirs"]
    # "Deployed with Docker" in the README, but no Dockerfile in the tree
    assert any("Docker" in claim for claim in facts["claims_to_verify"])


def test_notebook_outputs_are_readable_evidence():
    assert "Dice: 0.871" in notebook_text(FILES["notebooks/eval.ipynb"])


def test_detailed_readme_lowers_the_code_budget_but_code_is_still_read():
    llm = SequencedLLMProvider([])
    plan = plan_and_read(_snapshot(), llm, budget=30000)
    assert plan.code_budget == 18000  # detailed README -> 60%
    assert "src/train.py" in plan.files and "requirements.txt" in plan.files  # entry points + manifests always
    assert "node_modules/left-pad/index.js" not in plan.files
    assert llm.calls == []  # few enough files to read without asking the model to rank


def test_the_model_ranks_paths_only_and_invented_paths_are_dropped():
    snapshot = _snapshot()
    extra = {f"src/module_{i}.py": f"# module {i}\nSECRET_BODY_{i} = 1\n" for i in range(30)}
    snapshot.tree += [{"path": p, "size": len(t)} for p, t in extra.items()]
    snapshot.read_file = lambda path: {**FILES, **extra}.get(path, "")
    llm = SequencedLLMProvider([json.dumps({"paths": ["src/module_7.py", "invented/path.py"]})])
    plan = plan_and_read(snapshot, llm, budget=30000)
    assert plan.ranked_by_model == ["src/module_7.py"]
    assert plan.dropped_ranked == ["invented/path.py"]
    assert "src/module_7.py" in plan.files
    assert "SECRET_BODY" not in llm.calls[0]["prompt"]  # paths only, never contents


def test_ingest_repo_writes_a_verified_rich_record(monkeypatch):
    monkeypatch.setattr(github_ingest, "load_repo_snapshot", lambda gh, name, login="", emails=None: _snapshot())
    monkeypatch.setattr(github_ingest, "Github", lambda *a, **k: object())
    reading = SequencedLLMProvider([json.dumps({"paths": []})])
    draft = SequencedLLMProvider([_record_answer()])
    db = SessionLocal()
    try:
        init_db()
        item = github_ingest.ingest_repo(db, reading, "token", "owner/lesion-seg", draft_llm=draft, owner=("owner", []))
        record = item.extracted
        assert item.status == ProfileItemStatus.ANALYZED
        assert record["stack"] == item.facts["stack"]  # stack = manifest/import-derived list
        assert record["maturity"]["tests"] is True and record["maturity"]["ci"] is True  # from code
        assert record["maturity"]["packaged"] is False  # the model said True; facts win
        assert [r["claim"] for r in record["results"]] == ["Dice 0.87 on ISIC 2018 validation"]
        assert any("Used by 3 hospitals" in c for c in record["claims_to_verify"])
        assert any("Kubernetes" in c for c in record["claims_to_verify"])
        assert [u["tech"] for u in record["stack_used_for"]] == ["PyTorch"]
        assert len(record["open_questions"]) == 5
        assert item.source_version["head_sha"] == "abc123"
        assert len(draft.calls) == 1 and draft.calls[0]["json_schema"] is not None
    finally:
        db.close()


class _Repo:
    def __init__(self, sha):
        self.default_branch = "main"
        self._sha = sha

    def get_branch(self, name):
        class _B:
            pass

        branch = _B()
        branch.commit = _B()
        branch.commit.sha = self._sha
        return branch


class _Gh:
    def __init__(self, sha):
        self.sha = sha
        self.repo_calls = 0

    def get_repo(self, name):
        self.repo_calls += 1
        return _Repo(self.sha)


def test_check_for_changes_makes_no_llm_call_and_skips_unchanged_repos():
    db = SessionLocal()
    try:
        record_item(
            db, source_type="github_repo", source_ref="owner/unchanged", extracted={"purpose": "x", "stack": ["Go"], "role": "a", "highlights": []},
            source_urls=[], source_version={"pushed_at": "2026-10-01T00:00:00", "head_sha": "s1"},
        )
        record_item(
            db, source_type="github_repo", source_ref="owner/moved", extracted={"purpose": "y", "stack": ["Go"], "role": "a", "highlights": []},
            source_urls=[], source_version={"pushed_at": "2026-09-01T00:00:00", "head_sha": "old"},
        )
        record_item(
            db, source_type="github_repo", source_ref="owner/gone", extracted={"purpose": "z", "stack": ["Go"], "role": "a", "highlights": []},
            source_urls=[], source_version={"pushed_at": "x", "head_sha": "y"},
        )
        repos = [
            {"full_name": "owner/unchanged", "pushed_at": "2026-10-01T00:00:00", "suggested_include": True, "description": ""},
            {"full_name": "owner/moved", "pushed_at": "2026-10-05T00:00:00", "suggested_include": True, "description": ""},
            {"full_name": "owner/brand-new", "pushed_at": "2026-10-05T00:00:00", "suggested_include": True, "description": "new"},
        ]
        gh = _Gh("new-sha")
        report = repo_changes.check_for_changes(db, "token", gh=gh, repos=repos)
        changed = {c["full_name"]: c for c in report["changed"]}
        assert "owner/unchanged" not in changed and report["unchanged"] >= 1
        assert changed["owner/moved"]["new_sha"] == "new-sha"
        assert [n["full_name"] for n in report["new"]] == ["owner/brand-new"]
        assert "owner/gone" in [d["full_name"] for d in report["deleted"]]
        assert gh.repo_calls == 1  # only the repo whose pushed_at moved was looked up
        # no LLM provider is even passed to check_for_changes - zero calls by construction
    finally:
        db.query(ProfileItem).filter(ProfileItem.source_ref.in_(["owner/unchanged", "owner/moved", "owner/gone"])).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_reanalysis_proposes_a_diff_and_keeps_owner_fields(monkeypatch):
    monkeypatch.setattr(repo_changes, "load_repo_snapshot", lambda gh, name, login="", emails=None: _snapshot(head_sha="def456"))
    monkeypatch.setattr(repo_changes, "owner_identity", lambda gh: ("owner", []))
    monkeypatch.setattr(repo_changes, "load_changes_since", lambda gh, name, a, b: {"commits": ["add ONNX export"], "files": [{"path": "src/export.py", "status": "added", "changes": 40}]})
    db = SessionLocal()
    try:
        first = json.loads(_record_answer())
        from app.profile_builder.repo_record import verify_record

        facts = compute_facts(_snapshot())
        record = verify_record(first, quotable="", owner_facts=[], facts=facts, readme=README)
        item = record_item(db, source_type="github_repo", source_ref="owner/reanalyze", extracted=record, source_urls=[],
                           facts=facts, source_version={"pushed_at": "p", "head_sha": "abc123"})
        item.owner_edited_fields = ["one_liner"]
        item.extracted = {**item.extracted, "one_liner": "My own words."}
        item.owner_facts = [{"question": "Was this solo?", "answer": "Solo", "source": "owner"}]
        db.commit()

        reading = SequencedLLMProvider([json.dumps({"paths": []})])
        draft = SequencedLLMProvider([_record_answer(one_liner="Lesion segmentation with ONNX export.", domains=["medical imaging"])])
        repo_changes.reanalyze_repo(db, reading, draft, "token", item, gh=object())
        db.refresh(item)
        assert item.extracted["one_liner"] == "My own words."  # not overwritten
        assert item.pending_update["one_liner"] == "Lesion segmentation with ONNX export."
        assert item.pending_update["domains"] == ["medical imaging"]
        assert item.source_version["head_sha"] == "def456"
        assert item.owner_facts[0]["answer"] == "Solo"
        assert "add ONNX export" in draft.calls[0]["prompt"] and "PREVIOUS RECORD" in draft.calls[0]["prompt"]
        assert "Was this solo?" not in item.pending_update.get("open_questions", [])
    finally:
        db.query(ProfileItem).filter(ProfileItem.source_ref == "owner/reanalyze").delete(synchronize_session=False)
        db.commit()
        db.close()


def test_owner_identity_never_calls_the_emails_endpoint():
    class _Me:
        login, id, email = "hkln4", 42, "Owner@Example.com"

        def get_emails(self):
            raise AssertionError("GET /user/emails needs a scope fine-grained tokens lack")

    class _Gh:
        def get_user(self):
            return _Me()

    login, emails = github_ingest.owner_identity(_Gh())
    assert login == "hkln4"
    assert emails == ["hkln4@users.noreply.github.com", "42+hkln4@users.noreply.github.com", "owner@example.com"]
