"""Coverage ledger (CLAUDE.md section 3): every source item's status is tracked so
the owner can verify at a glance that nothing was silently dropped."""
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import ProfileItem, ProfileItemStatus


def record_item(
    db: Session, *, source_type: str, source_ref: str, extracted: dict, source_urls: list[str]
) -> ProfileItem:
    status = ProfileItemStatus.ANALYZED
    looks_empty = (
        extracted.get("purpose") == "unknown"
        and not extracted.get("stack")
        and extracted.get("role") == "unknown"
        and not extracted.get("highlights")
    )
    if looks_empty or "parse_error" in extracted:
        status = ProfileItemStatus.NEEDS_REVIEW

    item = ProfileItem(
        source_type=source_type,
        source_ref=source_ref,
        extracted=extracted,
        status=status,
        source_urls=source_urls,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def ledger_summary(db: Session) -> dict[str, dict[str, int]]:
    """{source_type: {analyzed, skipped, needs_review, total}}"""
    rows = (
        db.query(ProfileItem.source_type, ProfileItem.status, func.count(ProfileItem.id))
        .group_by(ProfileItem.source_type, ProfileItem.status)
        .all()
    )
    summary: dict[str, dict[str, int]] = {}
    for source_type, status, count in rows:
        bucket = summary.setdefault(
            source_type, {"analyzed": 0, "skipped": 0, "needs_review": 0, "total": 0}
        )
        bucket[status.value] = count
        bucket["total"] += count
    return summary
