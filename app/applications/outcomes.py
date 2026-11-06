"""Outcomes and the funnel (roadmap phase 7).

"Record outcome" sets the final state and archives what was sent - the
posting text, the letter, which CV file (snapshot) and the form channel -
so the record stays complete even if the posting disappears or a CV variant
is replaced. The funnel groups sent applications by source, channel, CV,
language and angle, with reply / interview rates and the median time to reply.
"""
from datetime import datetime, timezone
from statistics import median

from sqlalchemy.orm import Session

from app.applications.review import CONTACTED_STATES, set_application_state
from app.models import Application, ApplicationState, CV
from app.postings.gates import detect_language

REPLY_STATES = {ApplicationState.REPLIED, ApplicationState.INTERVIEW}
DIMENSIONS = ("source", "channel", "cv", "language", "angle")


def record_outcome(db: Session, application: Application, state: ApplicationState, note: str = "") -> Application:
    set_application_state(db, application, state)
    posting = application.job_posting
    application.outcome = {
        "result": state.value,
        "note": note.strip(),
        "at": datetime.now(timezone.utc).isoformat(),
        "archive": {
            "subject": application.subject,
            "letter": application.cover_letter_body,
            "sent_to": application.sent_to,
            "channel": application.channel,
            "cv_snapshot": application.cv_snapshot or {},
            "posting": {"title": posting.title, "url": posting.url, "text": posting.description_text} if posting else None,
        },
    }
    if posting is not None:
        from app.models import PostingStatus

        posting.status = PostingStatus.APPLIED
    db.commit()
    return application


def _source(application: Application) -> str:
    if application.job_posting is not None:
        return f"posting: {application.job_posting.source}"
    company = application.company
    if company is not None and company.technopark:
        return f"directory: {company.technopark}"
    return "company list"


def _cv(db: Session, application: Application) -> str:
    snapshot = application.cv_snapshot or {}
    if snapshot.get("generated_cv_id") or application.generated_cv_id:
        return "tailored CV"
    cv = db.get(CV, snapshot.get("cv_id")) if snapshot.get("cv_id") else application.cv
    return cv.name if cv else "unknown"


def _angle(application: Application) -> str:
    notes = (application.revision_history or [{}])[0]
    return notes.get("recipient_kind") or ("with angle" if notes.get("angles") else "no angle")


def _key(db: Session, application: Application, dimension: str) -> str:
    if dimension == "source":
        return _source(application)
    if dimension == "channel":
        return application.channel or "email"
    if dimension == "cv":
        return _cv(db, application)
    if dimension == "language":
        return detect_language(application.cover_letter_body) or "?"
    return _angle(application)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def funnel(db: Session) -> dict:
    """{totals, by: {dimension: [{key, sent, replies, interviews, reply_rate,
    interview_rate, median_days_to_reply}]}}"""
    sent = [a for a in db.query(Application).filter(Application.state.in_(CONTACTED_STATES)).all()]

    def summarize(group: list[Application]) -> dict:
        replies = [a for a in group if a.state in REPLY_STATES]
        interviews = [a for a in group if a.state == ApplicationState.INTERVIEW]
        days = [(_aware(a.replied_at) - _aware(a.sent_at)).total_seconds() / 86400 for a in replies if a.replied_at and a.sent_at]
        return {
            "sent": len(group),
            "replies": len(replies),
            "interviews": len(interviews),
            "reply_rate": round(len(replies) / len(group), 3) if group else 0.0,
            "interview_rate": round(len(interviews) / len(group), 3) if group else 0.0,
            "median_days_to_reply": round(median(days), 1) if days else None,
        }

    by = {}
    for dimension in DIMENSIONS:
        groups: dict[str, list[Application]] = {}
        for application in sent:
            groups.setdefault(_key(db, application, dimension), []).append(application)
        by[dimension] = sorted(({"key": key, **summarize(group)} for key, group in groups.items()), key=lambda row: -row["sent"])
    return {"totals": summarize(sent), "by": by}
