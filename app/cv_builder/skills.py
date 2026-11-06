"""The tech-stack list (roadmap phase 5), built by code - no LLM.

Sources, each kept as evidence on the skill:
- repo manifests / imports (ProfileItem.facts["stack"]) - kind "repo";
- the Professional Experience bullets of the owner's CV (technologies that
  appear only there, e.g. BigQuery, LightGBM, XTTS v2, FFmpeg) - "experience";
- manual additions - allowed, but with no other evidence they are marked
  "no evidence" as an interview reminder.

Groups follow the owner's CV: Programming Languages, Frameworks & Tools,
Technologies, Generative AI. A skill whose only evidence was a project the
owner deselected is flagged before it can appear on a CV.
"""
from sqlalchemy.orm import Session

from app.applications.grounding import technologies_in
from app.applications.synonyms import concepts_in, fold
from app.models import CVProject, ProfileItem, ProfileItemStatus, TechSkill

GROUPS = ("languages", "frameworks", "technologies", "genai")
GROUP_TITLES = {
    "en": {"languages": "Programming Languages", "frameworks": "Frameworks & Tools", "technologies": "Technologies", "genai": "Generative AI"},
    "tr": {"languages": "Programlama Dilleri", "frameworks": "Framework ve Araçlar", "technologies": "Teknolojiler", "genai": "Üretken Yapay Zeka"},
}
LANGUAGES = {"python", "sql", "c", "c++", "c#", "java", "javascript", "typescript", "go", "rust", "r", "matlab", "kotlin", "swift", "scala", "bash", "dart", "php", "ruby", "julia"}
GENAI = {
    "llms", "llm", "rag", "multimodal rag", "langchain", "langgraph", "llamaindex", "vector databases", "text embeddings",
    "ai agents", "ragas", "prompt engineering", "openai api", "anthropic api", "gemini api", "faiss", "chromadb", "qdrant",
    "pinecone", "milvus", "hugging face transformers", "sentence transformers", "peft", "vllm", "ollama", "diffusers",
}
# Concepts from the synonym table shown as "Technologies" (display names).
CONCEPT_DISPLAY = {
    "machine learning": "Machine Learning", "deep learning": "Deep Learning", "computer vision": "Computer Vision",
    "natural language processing": "Natural Language Processing", "segmentation": "Image Segmentation",
    "object detection": "Object Detection", "medical imaging": "Medical Imaging", "speech": "Speech Processing",
    "time series": "Time Series", "data engineering": "Data Engineering", "mlops": "MLOps", "big data": "Big Data",
    "recommendation": "Recommender Systems", "generative ai": "LLMs",
}
# General, well-known skills that belong on every CV. Anything else found in
# the code (Uvicorn, Pydantic, tiktoken, Gemini API ...) is "specific": listed
# here, but put on a CV only when the posting asks for it.
CORE_SKILLS = {
    "python", "sql", "c", "c++", "java", "javascript", "typescript", "go", "r",
    "pytorch", "tensorflow", "keras", "scikit-learn", "pandas", "numpy", "opencv", "matplotlib",
    "xgboost", "lightgbm", "hugging face transformers", "langchain", "fastapi", "flask", "django",
    "docker", "docker compose", "kubernetes", "git", "github actions", "linux", "postgresql", "mysql",
    "mongodb", "redis", "apache airflow", "pyspark", "kafka", "mlflow", "bigquery", "aws (boto3)",
    "react", "node.js", "ffmpeg", "gradio", "streamlit",
}
# A specific skill used in this many projects is core after all.
CORE_MIN_SOURCES = 3
LOCAL_LANGUAGE_NAMES = {"jupyter notebook": None, "html": None, "css": None, "shell": "Bash", "dockerfile": None}


def classify(name: str) -> str:
    key = fold(name).strip()
    if key in LANGUAGES:
        return "languages"
    if key in GENAI:
        return "genai"
    if name in CONCEPT_DISPLAY.values():
        return "technologies"
    return "frameworks"


def default_tier(name: str, group: str, evidence: list[dict]) -> str:
    if group in ("languages", "technologies") or fold(name) in CORE_SKILLS:
        return "core"
    sources = {(e.get("kind"), e.get("item_id")) for e in evidence if e.get("kind") != "manual"}
    return "core" if len(sources) >= CORE_MIN_SOURCES else "specific"


def _display(name: str) -> str | None:
    key = name.strip().casefold()
    if key in LOCAL_LANGUAGE_NAMES:
        return LOCAL_LANGUAGE_NAMES[key]
    return name.strip()


def collect_evidence(db: Session) -> dict[str, list[dict]]:
    """{skill name: [evidence]} from repos and CV experience entries."""
    found: dict[str, list[dict]] = {}

    def add(name: str | None, evidence: dict) -> None:
        if not name:
            return
        key = next((k for k in found if fold(k) == fold(name)), name)
        if evidence not in found.setdefault(key, []):
            found[key].append(evidence)

    items = db.query(ProfileItem).filter(ProfileItem.status == ProfileItemStatus.ANALYZED).all()
    for item in items:
        extracted = item.extracted or {}
        if item.source_type == "github_repo":
            evidence = {"kind": "repo", "ref": item.source_ref, "item_id": item.id}
            # Code-computed stack only. A v1 record's stack was written by a
            # model, so from it only names the technology vocabulary knows count.
            stack = (item.facts or {}).get("stack") or [t for t in extracted.get("stack") or [] if technologies_in(t)]
            for tech in stack:
                add(_display(tech), evidence)
            text = " ".join([extracted.get("one_liner", ""), extracted.get("approach", ""), " ".join(extracted.get("domains") or [])])
            for concept in concepts_in(text):
                add(CONCEPT_DISPLAY.get(concept), evidence)
        elif item.source_type in ("cv", "linkedin") and extracted.get("entry_type") == "experience":
            evidence = {"kind": "experience", "ref": extracted.get("role", item.source_ref), "item_id": item.id}
            text = " ".join(extracted.get("bullets") or [])
            for tech in technologies_in(text):
                add(tech, evidence)
            for concept in concepts_in(text):
                add(CONCEPT_DISPLAY.get(concept), evidence)
    return found


def importance(skill: TechSkill) -> int:
    """How much the owner's work leans on a skill: each project that uses it
    counts once, a job/internship twice, a skill he added by hand once."""
    refs = {}
    for e in skill.evidence or []:
        refs[(e.get("kind"), e.get("ref"))] = 2 if e.get("kind") == "experience" else 1
    return sum(refs.values()) + (1 if skill.manual else 0)


def by_importance(skills: list[TechSkill]) -> list[TechSkill]:
    """General skills first, then by how much they're used; ties keep the
    owner's order."""
    return sorted(skills, key=lambda s: (s.tier != "core", -importance(s), s.sort_order or 0, s.id or 0))


def rebuild_skills(db: Session) -> list[TechSkill]:
    """Refresh code-found evidence; manual skills and the owner's hide/group
    choices are kept."""
    evidence = collect_evidence(db)
    existing = {fold(s.name): s for s in db.query(TechSkill).all()}
    for name, items in evidence.items():
        skill = existing.get(fold(name))
        if skill is None:
            skill = TechSkill(name=name, group=classify(name))
            db.add(skill)
            existing[fold(name)] = skill
        manual_evidence = [e for e in skill.evidence or [] if e.get("kind") == "manual"]
        skill.evidence = items + manual_evidence
        if not skill.tier_set_by_owner and not skill.manual:
            skill.tier = default_tier(skill.name, skill.group, skill.evidence)
    for key, skill in existing.items():
        if key not in {fold(n) for n in evidence}:
            skill.evidence = [e for e in skill.evidence or [] if e.get("kind") == "manual"]
            if not skill.manual and not skill.evidence:
                db.delete(skill)
    db.commit()
    return db.query(TechSkill).order_by(TechSkill.group, TechSkill.sort_order, TechSkill.id).all()


def add_manual(db: Session, name: str, group: str = "") -> TechSkill:
    skill = next((s for s in db.query(TechSkill).all() if fold(s.name) == fold(name)), None)
    if skill is None:
        skill = TechSkill(name=name.strip(), group=group if group in GROUPS else classify(name), manual=True, tier="core",
                          evidence=[{"kind": "manual", "ref": "added by you"}])
        db.add(skill)
    else:
        skill.manual, skill.hidden = True, False
    db.commit()
    return skill


def evidence_status(skill: TechSkill, selected_item_ids: set[int]) -> str:
    """ok | no_evidence | evidence_removed (only backed by deselected projects)."""
    real = [e for e in skill.evidence or [] if e.get("kind") != "manual"]
    if not real:
        return "no_evidence"
    if all(e.get("kind") == "repo" and e.get("item_id") not in selected_item_ids for e in real):
        return "evidence_removed"
    return "ok"


def selected_project_item_ids(db: Session) -> set[int]:
    return {p.profile_item_id for p in db.query(CVProject).filter(CVProject.selected.is_(True)).all() if p.profile_item_id}


def import_cv_skills(db: Session, base_items: list[ProfileItem]) -> int:
    """Seed manual skills from the base CV's own Technical Skills section
    (comma-separated lines), so the owner starts from his current list."""
    added = 0
    for item in base_items:
        extracted = item.extracted or {}
        if extracted.get("entry_type") != "skills":
            continue
        for line in extracted.get("bullets") or []:
            for name in line.split(","):
                name = name.strip().strip(".")
                if 1 < len(name) <= 40 and ":" not in name:
                    before = db.query(TechSkill).count()
                    add_manual(db, name)
                    added += db.query(TechSkill).count() > before
    return added
