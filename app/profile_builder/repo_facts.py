"""Deterministic repo facts (roadmap phase 1, item 1) - computed by code from
a RepoSnapshot, never by the model, and stored as ProfileItem.facts.

The record's stack comes from here: real dependencies parsed from the
manifests (mapped to display names), the languages GitHub measured, imports
in the files that were read, and tooling the file tree proves (Docker, CI).
The model may describe what a technology was used for, but may not add one
this module didn't find (or that the README doesn't name).
"""
import configparser
import json
import re
import tomllib
from collections import Counter
from pathlib import PurePosixPath

from app.profile_builder.repo_snapshot import RepoSnapshot

# Paths that are never the owner's code.
VENDORED_RE = re.compile(
    r"(^|/)(node_modules|vendor|third_party|external|\.venv|venv|env|site-packages|dist|build|"
    r"\.next|\.nuxt|target|out|coverage|__pycache__|\.git|\.idea|\.vscode|migrations|bower_components)(/|$)"
    r"|\.min\.(js|css)$|\.lock$|-lock\.json$|\.map$",
    re.IGNORECASE,
)
MANIFEST_RE = re.compile(
    r"(^|/)(requirements[^/]*\.txt|pyproject\.toml|setup\.cfg|setup\.py|environment\.ya?ml|package\.json|"
    r"go\.mod|Cargo\.toml|pom\.xml|build\.gradle(\.kts)?|Dockerfile[^/]*|docker-compose[^/]*\.ya?ml|compose\.ya?ml)$",
    re.IGNORECASE,
)
TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+\.py$|_test\.(py|go)$|\.(test|spec)\.(js|jsx|ts|tsx)$|Test\.java$",
    re.IGNORECASE,
)
CI_PATH_RE = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$|^\.gitlab-ci\.yml$|^\.circleci/|^azure-pipelines\.yml$|^Jenkinsfile$", re.IGNORECASE)
DEPLOY_PATH_RE = re.compile(
    r"(^|/)(Dockerfile[^/]*|docker-compose[^/]*\.ya?ml|Procfile|vercel\.json|netlify\.toml|fly\.toml|render\.yaml|"
    r"app\.yaml|serverless\.ya?ml)$|(^|/)(k8s|kubernetes|helm|charts|deploy|deployment)/",
    re.IGNORECASE,
)

# Dependency name (lowercase, normalized "-") -> display name.
DEPENDENCY_DISPLAY = {
    "torch": "PyTorch", "pytorch": "PyTorch", "torchvision": "PyTorch", "lightning": "PyTorch Lightning",
    "pytorch-lightning": "PyTorch Lightning", "tensorflow": "TensorFlow", "tensorflow-gpu": "TensorFlow",
    "keras": "Keras", "jax": "JAX", "flax": "JAX", "scikit-learn": "scikit-learn", "sklearn": "scikit-learn",
    "numpy": "NumPy", "pandas": "pandas", "polars": "Polars", "scipy": "SciPy", "matplotlib": "Matplotlib",
    "seaborn": "Seaborn", "plotly": "Plotly", "opencv-python": "OpenCV", "opencv-python-headless": "OpenCV",
    "opencv-contrib-python": "OpenCV", "opencv": "OpenCV", "pillow": "Pillow", "scikit-image": "scikit-image",
    "albumentations": "Albumentations", "ultralytics": "YOLO (Ultralytics)", "monai": "MONAI",
    "segmentation-models-pytorch": "segmentation-models-pytorch", "timm": "timm", "detectron2": "Detectron2",
    "mmdet": "MMDetection", "mediapipe": "MediaPipe", "onnx": "ONNX", "onnxruntime": "ONNX Runtime",
    "onnxruntime-gpu": "ONNX Runtime", "tensorrt": "TensorRT", "openvino": "OpenVINO",
    "transformers": "Hugging Face Transformers", "datasets": "Hugging Face Datasets", "peft": "PEFT",
    "accelerate": "Hugging Face Accelerate", "sentence-transformers": "Sentence Transformers",
    "diffusers": "Diffusers", "langchain": "LangChain", "langchain-core": "LangChain", "langchain-community": "LangChain",
    "langgraph": "LangGraph", "llama-index": "LlamaIndex", "openai": "OpenAI API", "anthropic": "Anthropic API",
    "google-genai": "Gemini API", "google-generativeai": "Gemini API", "ollama": "Ollama", "vllm": "vLLM",
    "faiss-cpu": "FAISS", "faiss-gpu": "FAISS", "chromadb": "ChromaDB", "qdrant-client": "Qdrant",
    "pinecone-client": "Pinecone", "pymilvus": "Milvus", "tiktoken": "tiktoken", "spacy": "spaCy", "nltk": "NLTK",
    "gensim": "Gensim", "xgboost": "XGBoost", "lightgbm": "LightGBM", "catboost": "CatBoost", "optuna": "Optuna",
    "mlflow": "MLflow", "wandb": "Weights & Biases", "dvc": "DVC", "tensorboard": "TensorBoard",
    "fastapi": "FastAPI", "flask": "Flask", "django": "Django", "djangorestframework": "Django REST Framework",
    "starlette": "Starlette", "uvicorn": "Uvicorn", "gunicorn": "Gunicorn", "streamlit": "Streamlit",
    "gradio": "Gradio", "pydantic": "Pydantic", "sqlalchemy": "SQLAlchemy", "alembic": "Alembic",
    "psycopg2": "PostgreSQL", "psycopg2-binary": "PostgreSQL", "psycopg": "PostgreSQL", "asyncpg": "PostgreSQL",
    "pymysql": "MySQL", "mysqlclient": "MySQL", "mysql-connector-python": "MySQL", "pymongo": "MongoDB",
    "motor": "MongoDB", "redis": "Redis", "celery": "Celery", "kafka-python": "Kafka", "confluent-kafka": "Kafka",
    "pika": "RabbitMQ", "apache-airflow": "Apache Airflow", "airflow": "Apache Airflow", "pyspark": "PySpark",
    "dask": "Dask", "duckdb": "DuckDB", "dbt-core": "dbt", "google-cloud-bigquery": "BigQuery",
    "google-cloud-storage": "Google Cloud Storage", "boto3": "AWS (boto3)", "azure-storage-blob": "Azure Storage",
    "elasticsearch": "Elasticsearch", "requests": "Requests", "httpx": "HTTPX", "aiohttp": "aiohttp",
    "beautifulsoup4": "BeautifulSoup", "scrapy": "Scrapy", "selenium": "Selenium", "playwright": "Playwright",
    "pytest": "pytest", "docker": "Docker SDK", "grpcio": "gRPC", "protobuf": "Protocol Buffers",
    "opencv-js": "OpenCV", "pyqt5": "PyQt", "pyqt6": "PyQt", "pyside6": "PySide", "tkinter": "Tkinter",
    "librosa": "librosa", "ffmpeg-python": "FFmpeg", "moviepy": "MoviePy", "whisper": "Whisper",
    "openai-whisper": "Whisper", "faster-whisper": "Whisper", "tts": "Coqui TTS", "coqui-tts": "Coqui TTS",
    "pyannote-audio": "pyannote.audio", "rasterio": "Rasterio", "geopandas": "GeoPandas", "shapely": "Shapely",
    "nibabel": "NiBabel", "pydicom": "pydicom", "simpleitk": "SimpleITK",
    # JavaScript / TypeScript
    "react": "React", "react-dom": "React", "next": "Next.js", "vue": "Vue", "nuxt": "Nuxt", "svelte": "Svelte",
    "@angular/core": "Angular", "express": "Express", "@nestjs/core": "NestJS", "typescript": "TypeScript",
    "tailwindcss": "Tailwind CSS", "vite": "Vite", "webpack": "webpack", "jest": "Jest", "vitest": "Vitest",
    "prisma": "Prisma", "@prisma/client": "Prisma", "mongoose": "MongoDB", "pg": "PostgreSQL", "socket.io": "Socket.IO",
    "three": "Three.js", "d3": "D3.js", "redux": "Redux", "@reduxjs/toolkit": "Redux", "axios": "Axios",
    "electron": "Electron", "react-native": "React Native", "@tensorflow/tfjs": "TensorFlow.js",
    "langchain-js": "LangChain", "@langchain/core": "LangChain",
    # Go / Rust / Java (module path or crate/artifact, lowercase)
    "github.com/gin-gonic/gin": "Gin", "github.com/gofiber/fiber": "Fiber", "github.com/labstack/echo": "Echo",
    "gorm.io/gorm": "GORM", "tokio": "Tokio", "actix-web": "Actix Web", "axum": "Axum", "serde": "Serde",
    "spring-boot-starter-web": "Spring Boot", "spring-boot-starter": "Spring Boot", "hibernate-core": "Hibernate",
}
# import name -> display (when it differs from the dependency name)
IMPORT_DISPLAY = {
    "cv2": "OpenCV", "sklearn": "scikit-learn", "PIL": "Pillow", "torch": "PyTorch", "tensorflow": "TensorFlow",
    "keras": "Keras", "numpy": "NumPy", "pandas": "pandas", "transformers": "Hugging Face Transformers",
    "fastapi": "FastAPI", "flask": "Flask", "django": "Django", "sqlalchemy": "SQLAlchemy", "ultralytics": "YOLO (Ultralytics)",
    "langchain": "LangChain", "openai": "OpenAI API", "anthropic": "Anthropic API", "pyspark": "PySpark",
    "airflow": "Apache Airflow", "xgboost": "XGBoost", "lightgbm": "LightGBM", "mlflow": "MLflow",
    "streamlit": "Streamlit", "gradio": "Gradio", "redis": "Redis", "pymongo": "MongoDB", "psycopg2": "PostgreSQL",
    "matplotlib": "Matplotlib", "scipy": "SciPy", "skimage": "scikit-image", "albumentations": "Albumentations",
    "monai": "MONAI", "timm": "timm", "onnxruntime": "ONNX Runtime", "faiss": "FAISS", "chromadb": "ChromaDB",
    "spacy": "spaCy", "nltk": "NLTK", "google.cloud.bigquery": "BigQuery", "boto3": "AWS (boto3)",
    "celery": "Celery", "kafka": "Kafka", "selenium": "Selenium", "bs4": "BeautifulSoup", "whisper": "Whisper",
    "librosa": "librosa", "mediapipe": "MediaPipe", "pydantic": "Pydantic",
}
COMPOSE_IMAGE_DISPLAY = {
    "postgres": "PostgreSQL", "mysql": "MySQL", "mariadb": "MariaDB", "mongo": "MongoDB", "redis": "Redis",
    "rabbitmq": "RabbitMQ", "kafka": "Kafka", "elasticsearch": "Elasticsearch", "nginx": "nginx",
    "minio": "MinIO", "grafana": "Grafana", "prometheus": "Prometheus", "airflow": "Apache Airflow",
}
LANGUAGE_MIN_SHARE = 0.05
# README feature words -> what the tree must contain to back them.
README_CLAIMS = {
    "docker": ("Docker", lambda f: bool(f["deployment_files"]) and any("docker" in p.lower() for p in f["deployment_files"])),
    "github actions": ("CI", lambda f: bool(f["ci_workflows"])),
    "ci/cd": ("CI", lambda f: bool(f["ci_workflows"])),
    "continuous integration": ("CI", lambda f: bool(f["ci_workflows"])),
    "unit test": ("tests", lambda f: f["tests"]["present"]),
    "pytest": ("tests", lambda f: f["tests"]["present"]),
    "test coverage": ("tests", lambda f: f["tests"]["present"]),
    "kubernetes": ("Kubernetes", lambda f: any(re.search(r"k8s|kubernetes|helm|charts", p, re.I) for p in f["deployment_files"])),
    "deployed": ("deployment", lambda f: bool(f["deployment_files"]) or bool(f["homepage"])),
    "live demo": ("deployment", lambda f: bool(f["homepage"])),
    "rest api": ("API", lambda f: bool(set(f["stack"]) & {"FastAPI", "Flask", "Django", "Django REST Framework", "Express", "NestJS", "Gin", "Spring Boot"})),
}

_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._\-]*)")
_PY_IMPORT_RE = re.compile(r"^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import|import\s+([A-Za-z_][\w.]*))", re.MULTILINE)
_JS_IMPORT_RE = re.compile(r"""(?:from\s+|require\(\s*)['"]([^'".][^'"]*)['"]""")


def normalize_dep(name: str) -> str:
    return re.sub(r"[_.]+", "-", name.strip().lower())


def _req_names(text: str) -> list[str]:
    names = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http")):
            continue
        match = _REQ_NAME_RE.match(line)
        if match:
            names.append(match.group(1))
    return names


def parse_manifest(path: str, text: str) -> list[str]:
    """Dependency names declared in one manifest file."""
    name = PurePosixPath(path).name.lower()
    try:
        if name.startswith("requirements") and name.endswith(".txt"):
            return _req_names(text)
        if name == "pyproject.toml":
            data = tomllib.loads(text)
            deps = list(data.get("project", {}).get("dependencies", []))
            for group in data.get("project", {}).get("optional-dependencies", {}).values():
                deps += group
            poetry = data.get("tool", {}).get("poetry", {})
            deps += [k for k in poetry.get("dependencies", {}) if k.lower() != "python"]
            for group in poetry.get("group", {}).values():
                deps += list(group.get("dependencies", {}))
            return _req_names("\n".join(deps))
        if name == "setup.cfg":
            parser = configparser.ConfigParser()
            parser.read_string(text)
            return _req_names(parser.get("options", "install_requires", fallback=""))
        if name == "setup.py":
            block = re.search(r"install_requires\s*=\s*\[(.*?)\]", text, re.DOTALL)
            return _req_names("\n".join(re.findall(r"['\"]([^'\"]+)['\"]", block.group(1)))) if block else []
        if name in ("environment.yml", "environment.yaml"):
            deps = []
            for line in text.splitlines():
                match = re.match(r"^\s*-\s*([A-Za-z0-9][A-Za-z0-9._\-]*)", line)
                if match and match.group(1).lower() not in ("pip", "python"):
                    deps.append(match.group(1))
            return deps
        if name == "package.json":
            data = json.loads(text)
            return list(data.get("dependencies", {})) + list(data.get("devDependencies", {}))
        if name == "go.mod":
            return re.findall(r"^\s*(?:require\s+)?([a-z0-9.\-]+\.[a-z]+/[^\s]+)\s+v[\d.]", text, re.MULTILINE)
        if name == "cargo.toml":
            data = tomllib.loads(text)
            return list(data.get("dependencies", {})) + list(data.get("dev-dependencies", {}))
        if name == "pom.xml":
            return re.findall(r"<dependency>.*?<artifactId>\s*([^<\s]+)\s*</artifactId>", text, re.DOTALL)
        if name.startswith("build.gradle"):
            return [m.split(":")[1] for m in re.findall(r"""['"]([\w.\-]+:[\w.\-]+)(?::[^'"]*)?['"]""", text)]
    except (ValueError, KeyError, configparser.Error, tomllib.TOMLDecodeError):
        return []
    return []


def _compose_services(text: str) -> list[str]:
    images = re.findall(r"^\s*image:\s*['\"]?([^\s'\":]+)", text, re.MULTILINE)
    found = []
    for image in images:
        base = image.rsplit("/", 1)[-1].lower()
        for key, display in COMPOSE_IMAGE_DISPLAY.items():
            if base.startswith(key):
                found.append(display)
    return found


def display_name(dependency: str) -> str | None:
    key = normalize_dep(dependency)
    return DEPENDENCY_DISPLAY.get(key) or DEPENDENCY_DISPLAY.get(dependency.strip().lower())


def imports_in(path: str, text: str) -> set[str]:
    """Display names of known libraries imported by one source file."""
    found = set()
    if path.endswith(".py") or path.endswith(".ipynb"):
        for match in _PY_IMPORT_RE.finditer(text):
            module = match.group(1) or match.group(2)
            for candidate in (module, module.split(".")[0], ".".join(module.split(".")[:3])):
                if candidate in IMPORT_DISPLAY:
                    found.add(IMPORT_DISPLAY[candidate])
                    break
    elif re.search(r"\.(js|jsx|ts|tsx|mjs)$", path):
        for module in _JS_IMPORT_RE.findall(text):
            display = display_name(module) or display_name(module.split("/")[0])
            if display:
                found.add(display)
    return found


def _owner_commit_share(snapshot: RepoSnapshot) -> dict:
    commits = snapshot.commits
    if not commits:
        return {"scanned": 0, "by_owner": 0, "share": None}
    login = snapshot.owner_login.lower()
    emails = set(snapshot.owner_emails)
    mine = sum(
        1
        for c in commits
        if (login and (c.get("author_login") or "").lower() == login) or (c.get("author_email") or "").lower() in emails
    )
    return {"scanned": len(commits), "by_owner": mine, "share": round(mine / len(commits), 2)}


def compute_facts(snapshot: RepoSnapshot, read_texts: dict[str, str] | None = None) -> dict:
    """All deterministic facts for one repo. `read_texts` are source files the
    reading plan already read (their imports count as evidence)."""
    paths = [entry["path"] for entry in snapshot.tree]
    own_paths = [p for p in paths if not VENDORED_RE.search(p)]

    manifests = {}
    dependencies: list[str] = []
    stack: list[str] = []

    def add_stack(name: str | None) -> None:
        if name and name not in stack:
            stack.append(name)

    total_bytes = sum(snapshot.languages.values()) or 1
    languages = sorted(snapshot.languages.items(), key=lambda kv: -kv[1])
    for language, size in languages:
        if size / total_bytes >= LANGUAGE_MIN_SHARE and language not in ("HTML", "CSS", "Makefile", "Shell", "Batchfile", "Dockerfile"):
            add_stack(language)

    compose_services: list[str] = []
    for path in own_paths:
        if not MANIFEST_RE.search(path) or path.count("/") > 2:
            continue
        text = snapshot.read_file(path)
        if not text:
            continue
        base = PurePosixPath(path).name.lower()
        if base.startswith("dockerfile"):
            manifests[path] = ["Docker"]
            add_stack("Docker")
            if re.search(r"^\s*FROM\s+nvidia/cuda", text, re.IGNORECASE | re.MULTILINE):
                add_stack("CUDA")
            continue
        if "compose" in base:
            services = _compose_services(text)
            manifests[path] = ["Docker Compose", *services]
            add_stack("Docker")
            add_stack("Docker Compose")
            compose_services += services
            for service in services:
                add_stack(service)
            continue
        names = parse_manifest(path, text)
        manifests[path] = names
        for name in names:
            if name not in dependencies:
                dependencies.append(name)
            add_stack(display_name(name))

    imported: set[str] = set()
    for path, text in (read_texts or {}).items():
        imported |= imports_in(path, text)
    for name in sorted(imported):
        add_stack(name)

    tests = [p for p in own_paths if TEST_PATH_RE.search(p)]
    ci = [p for p in paths if CI_PATH_RE.search(p)]
    deploy = [p for p in own_paths if DEPLOY_PATH_RE.search(p)]
    if ci:
        add_stack("GitHub Actions" if any(p.startswith(".github/workflows/") for p in ci) else "CI")
    extensions = Counter(PurePosixPath(p).suffix.lower() or PurePosixPath(p).name for p in own_paths)
    top_dirs = sorted({p.split("/", 1)[0] for p in own_paths if "/" in p})

    packaged = ""
    for path, names in manifests.items():
        base = PurePosixPath(path).name.lower()
        text = snapshot.read_file(path) if base in ("pyproject.toml", "package.json") else ""
        if base == "pyproject.toml":
            try:
                packaged = tomllib.loads(text).get("project", {}).get("name", "") or packaged
            except tomllib.TOMLDecodeError:
                pass
        elif base == "package.json":
            try:
                data = json.loads(text)
                if not data.get("private") and data.get("name"):
                    packaged = data["name"]
            except ValueError:
                pass

    dates = sorted(c["date"] for c in snapshot.commits if c.get("date"))
    facts = {
        "languages_bytes": dict(languages),
        "top_level_dirs": top_dirs[:30],
        "file_counts": dict(extensions.most_common(12)),
        "notebooks": sum(1 for p in own_paths if p.endswith(".ipynb")),
        "tests": {"present": bool(tests), "count": len(tests), "examples": tests[:5]},
        "ci_workflows": ci[:10],
        "deployment_files": deploy[:10],
        "license": snapshot.license,
        "packaged": packaged,
        "homepage": snapshot.homepage,
        "manifests": manifests,
        "dependencies": dependencies[:200],
        "imports": sorted(imported),
        "compose_services": compose_services,
        "stack": stack,
        "activity": {
            "first_commit": dates[0] if dates else "",
            "last_commit": dates[-1] if dates else "",
            "total_commits": snapshot.total_commits or len(snapshot.commits),
            "owner_commits": _owner_commit_share(snapshot),
            "contributors": snapshot.contributors,
            "releases": snapshot.releases,
            "tags": snapshot.tags,
            "stars": snapshot.stars,
            "forks": snapshot.forks,
        },
        "topics": snapshot.topics,
    }
    facts["claims_to_verify"] = readme_claim_mismatches(snapshot.readme, facts)
    facts["maturity"] = {
        "tests": facts["tests"]["present"],
        "ci": bool(ci),
        "deployed": bool(deploy) or bool(snapshot.homepage),
        "packaged": bool(packaged),
    }
    return facts


def readme_claim_mismatches(readme: str, facts: dict) -> list[str]:
    """README feature words the file tree doesn't back."""
    lowered = (readme or "").casefold()
    mismatches = []
    seen = set()
    for word, (feature, backed) in README_CLAIMS.items():
        if word in lowered and feature not in seen and not backed(facts):
            seen.add(feature)
            mismatches.append(f"README mentions {word!r} but the repo has no files backing {feature}.")
    return mismatches


def allowed_technologies(facts: dict, readme: str) -> set[str]:
    """Technologies the record may name: the deterministic stack plus anything
    the README names. Compared casefolded."""
    allowed = {name.casefold() for name in facts.get("stack", [])}
    allowed |= {normalize_dep(d) for d in facts.get("dependencies", [])}
    allowed |= {name.casefold() for name in facts.get("languages_bytes", {})}
    return allowed | {"__readme__:" + (readme or "").casefold()}


def technology_is_backed(tech: str, allowed: set[str]) -> bool:
    key = tech.strip().casefold()
    if not key:
        return False
    if key in allowed or normalize_dep(key) in allowed:
        return True
    # "Hugging Face" is backed by "Hugging Face Transformers": every word of the
    # claimed name must be in a found name ("Java" is not backed by "JavaScript",
    # "PyTorch Lightning" is not backed by "PyTorch").
    key_words = set(re.findall(r"[\w+#.]+", key))
    for name in allowed:
        if name.startswith("__readme__:"):
            continue
        name_words = set(re.findall(r"[\w+#.]+", name))
        if key_words and key_words <= name_words:
            return True
    readme = next((a[len("__readme__:"):] for a in allowed if a.startswith("__readme__:")), "")
    return bool(readme) and re.search(rf"(?<![\w]){re.escape(key)}(?![\w])", readme) is not None
