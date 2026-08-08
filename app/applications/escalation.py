"""Employee fallback (CLAUDE.md section 5 step 7): when the company's generic
address doesn't reply past the threshold, the owner can send a tailored email to a
specific, manually-entered employee contact. Reuses the application lifecycle."""
from sqlalchemy.orm import Session

from app.applications.drafting import _resolve_cv, draft_application
from app.llm.base import LLMProvider
from app.models import Application, ApplicationState, Contact, ContactState


def create_escalation_draft(db: Session, llm: LLMProvider, contact: Contact) -> Application:
    company = contact.company
    result = draft_application(db, llm, company)
    cv = _resolve_cv(db, result.get("cv_choice_name", ""))

    application = Application(
        company_id=company.id,
        cv_id=cv.id if cv else None,
        target_position=result.get("target_position", ""),
        subject=f"{result.get('subject', '')} (Attn: {contact.name})",
        cover_letter_body=result.get("cover_letter_body", ""),
        sources=result.get("sources", []),
        revision_history=[{"reasoning": result.get("reasoning", ""), "escalation_for_contact": contact.name}],
        state=ApplicationState.PENDING_APPROVAL,
    )
    db.add(application)
    db.commit()
    db.refresh(application)

    contact.application_id = application.id
    contact.state = ContactState.DRAFT
    db.commit()
    return application
