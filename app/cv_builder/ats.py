"""ATS check on a rendered CV (roadmap phase 5): read the PDF's text layer
back with pypdf - what a parser actually sees - and verify:
- name, email and phone are present, in that reading order;
- every section heading is found;
- the keyword report matches the extracted text (a keyword "covered" by the
  source but missing from the text layer is a failure);
- every word printed is in the text layer as written (catches "mid-LTV"
  coming out as "midLTV", or kerning splitting "H asan");
- the page limit holds.
A failing check blocks approval of that CV - never of the letter.
"""
import io
import re

from pypdf import PdfReader

from app.applications.synonyms import fold
from app.config import get_settings
from app.cv_builder.assemble import keyword_report


def text_layer(pdf: bytes) -> tuple[str, int]:
    reader = PdfReader(io.BytesIO(pdf))
    return "\n".join(page.extract_text() or "" for page in reader.pages), len(reader.pages)


def _words(text: str) -> list[str]:
    return re.findall(r"[\w][\w\-.+#/@]*[\w+#]|\w", text)


def check(pdf: bytes, data: dict, keywords: list[str]) -> dict:
    text, pages = text_layer(pdf)
    flat = re.sub(r"\s+", " ", text)
    folded = fold(flat)
    checks = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "passed": ok, "detail": detail})

    contact = {c["text"] for c in data.get("contacts", [])}
    email = next((c for c in contact if "@" in c), "")
    phone = next((c for c in contact if re.fullmatch(r"[+\d\s()-]{7,}", c)), "")
    positions = [folded.find(fold(v)) for v in (data.get("name", ""), email, phone) if v]
    add("contact_present", all(p >= 0 for p in positions) and bool(data.get("name")), "name / email / phone")
    add("contact_order", positions == sorted(positions) and all(p >= 0 for p in positions))
    missing_headings = [s["title"] for s in data.get("sections", []) if fold(s["title"]) not in folded]
    add("headings_found", not missing_headings, ", ".join(missing_headings))

    source_text = " ".join(_source(data))
    expected = keyword_report(keywords, source_text)
    seen = keyword_report(keywords, flat)
    lost = [k for k in expected["covered"] if k not in seen["covered"]]
    add("keywords_in_text_layer", not lost, ", ".join(lost))

    layer_words = set(_words(folded))
    diffs = sorted({w for s in _source(data) for w in _words(fold(s)) if w not in layer_words and len(w) > 1})
    add("words_intact", not diffs, ", ".join(diffs[:15]))
    limit = get_settings().cv_page_limit
    add("page_limit", pages <= limit, f"{pages} / {limit}")
    return {
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
        "page_count": pages,
        "word_diffs": diffs,
        "keyword_report": seen,
    }


def _source(data: dict) -> list[str]:
    from app.cv_builder.render import source_strings

    return source_strings(data)
