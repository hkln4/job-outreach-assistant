"""Consistency check across the owner's own sources (roadmap phase 5): the
same metric of the same project must not carry different numbers on the
portfolio, the CV, the profile record and the bullet bank (e.g. "Dice 0.93"
on the portfolio, "Dice 0.931" on the CV is fine; "Dice 0.89" is flagged).
Code only - metric names and numbers are matched by regex."""
import json
import re

from sqlalchemy.orm import Session

from app.applications.signature import owner_identity
from app.applications.synonyms import fold
from app.models import CVProject, ProfileItem, ProfileItemStatus

METRIC_RE = re.compile(
    r"\b(dice|iou|miou|map|mae|rmse|mse|r2|r²|accuracy|doğruluk|precision|recall|f1|auc|roc-auc|faithfulness|"
    r"answer relevancy|bleu|rouge|wer|latency|throughput)\b[^0-9\n]{0,12}(\d+(?:[.,]\d+)?)\s*(%?)",
    re.IGNORECASE,
)


def metrics_in(text: str) -> dict[str, set[float]]:
    found: dict[str, set[float]] = {}
    for name, number, percent in METRIC_RE.findall(text or ""):
        value = float(number.replace(",", "."))
        if percent and value > 1:
            value = value / 100
        found.setdefault(fold(name).replace("r²", "r2"), set()).add(round(value, 2))
    return found


def _texts_for(db: Session, project: CVProject, portfolio_root: str) -> dict[str, str]:
    """{source label: text} for one project."""
    key = fold(project.name)
    texts = {}
    item = project.profile_item
    if item is not None:
        texts["record"] = json.dumps(item.extracted or {}, ensure_ascii=False) + " " + " ".join(f.get("answer", "") for f in item.owner_facts or [])
    texts["bullets"] = " ".join(b.text for b in project.bullets)
    for other in db.query(ProfileItem).filter(ProfileItem.status == ProfileItemStatus.ANALYZED).all():
        body = json.dumps(other.extracted or {}, ensure_ascii=False)
        if key and key in fold(body + " " + other.source_ref):
            if other.source_type == "site" and portfolio_root and other.source_ref.startswith(portfolio_root):
                texts.setdefault("portfolio", "")
                texts["portfolio"] += " " + body
            elif other.source_type in ("cv", "linkedin"):
                texts.setdefault("cv", "")
                texts["cv"] += " " + body
    return texts


def check_consistency(db: Session) -> list[dict]:
    """[{project, metric, values: {source: [numbers]}}] where sources disagree."""
    portfolio = (owner_identity(db).get("portfolio_url") or "").rstrip("/")
    issues = []
    for project in db.query(CVProject).filter(CVProject.selected.is_(True)).all():
        per_source = {label: metrics_in(text) for label, text in _texts_for(db, project, portfolio).items()}
        for metric in sorted({m for values in per_source.values() for m in values}):
            values = {label: sorted(vals[metric]) for label, vals in per_source.items() if metric in vals}
            distinct = {v for vals in values.values() for v in vals}
            # rounding differences (0.93 vs 0.931 -> both 0.93) are not contradictions
            if len(values) > 1 and len(distinct) > 1 and not all(set(v) & set(next(iter(values.values()))) for v in values.values()):
                issues.append({"project": project.name, "metric": metric, "values": values})
    return issues
