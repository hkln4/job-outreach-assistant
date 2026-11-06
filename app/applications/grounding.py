"""Deterministic grounding audit (roadmap phase 4, item 6): every number,
year and technology name in the letter must appear in the evidence records,
the profile, the owner's stated facts or the company text. Misses are listed
in "check before sending" - the letter is never changed by this.
"""
import re

from app.applications.synonyms import fold
from app.profile_builder.repo_facts import DEPENDENCY_DISPLAY, IMPORT_DISPLAY

NUMBER_RE = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)\s*(%|x|k|m|gb|mb|ms|s|saat|gün|yıl|years?)?(?![\w])", re.IGNORECASE)
EXTRA_TECH = {
    "Python", "Java", "JavaScript", "TypeScript", "C++", "C#", "Go", "Rust", "SQL", "R", "MATLAB", "Kotlin", "Swift",
    "Docker", "Kubernetes", "AWS", "GCP", "Azure", "Git", "Linux", "Spark", "Hadoop", "Kafka", "Airflow", "BigQuery",
    "LightGBM", "XGBoost", "XTTS", "FFmpeg", "CUDA", "YOLO", "U-Net", "RAG", "LLM", "GPT", "BERT", "OpenCV",
    "PyTorch", "TensorFlow", "Keras", "FastAPI", "Flask", "Django", "React", "Node.js", "PostgreSQL", "MongoDB", "Redis",
    "Gradio", "Streamlit", "LangChain", "Hugging Face", "scikit-learn", "Pandas", "NumPy", "Tableau", "Power BI",
}
TECH_VOCABULARY = sorted(set(DEPENDENCY_DISPLAY.values()) | set(IMPORT_DISPLAY.values()) | EXTRA_TECH, key=len, reverse=True)
# Small numbers are mostly grammar ("one project", "2 years" is still checked via the unit).
MIN_CHECKED_NUMBER = 3


def _contains(haystack: str, needle: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(fold(needle))}(?![\w])", haystack) is not None


def technologies_in(text: str) -> list[str]:
    folded = fold(text)
    found = []
    for tech in TECH_VOCABULARY:
        if len(tech) <= 1:
            continue
        if _contains(folded, tech) and not any(fold(tech) in fold(other) for other in found):
            found.append(tech)
    return found


def numbers_in(text: str) -> list[str]:
    found = []
    for match in NUMBER_RE.finditer(text or ""):
        value = match.group(1)
        try:
            number = float(value.replace(",", "."))
        except ValueError:
            continue
        if number < MIN_CHECKED_NUMBER and not match.group(2):
            continue
        if value not in found:
            found.append(value)
    return found


def grounding_misses(letter: str, sources_text: str) -> dict:
    """{"numbers": [...], "technologies": [...]} the sources don't contain."""
    haystack = fold(sources_text)
    numeric_haystack = haystack.replace(",", ".")
    numbers = [n for n in numbers_in(letter) if n.replace(",", ".") not in numeric_haystack]
    technologies = [t for t in technologies_in(letter) if not _contains(haystack, t)]
    return {"numbers": numbers, "technologies": technologies}
