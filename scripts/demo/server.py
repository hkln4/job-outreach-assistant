"""Demo server for the README GIF.

Runs the real app UI against an isolated demo database, with:
- a canned-response LLM provider (no API key, no network),
- a fake Gmail sender (nothing is ever actually sent),
- a fake web fetcher (no real HTTP requests to company sites),
so every flow can be clicked through live and deterministically.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(BASE))

# Isolated demo DB + no send delay. Must be set before any app import.
db_path = os.path.join(BASE, "demo.db")
if os.path.exists(db_path):
    os.remove(db_path)
os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
os.environ["SEND_DELAY_SECONDS"] = "0"
os.environ["LLM_PROVIDER"] = "gemini"
os.environ["GEMINI_API_KEY"] = "demo-key-unused"

sys.path.insert(0, REPO)
os.chdir(REPO)  # templates/static are referenced by relative path

from app.db import SessionLocal, init_db  # noqa: E402
from app.llm.base import LLMProvider, LLMResponse  # noqa: E402
from app.models import (  # noqa: E402
    CV,
    Application,
    ApplicationState,
    Company,
    CompanyState,
    Contact,
    Preference,
    PreferenceType,
    Profile,
    ProfileItem,
    ProfileItemStatus,
)

# --- Fakes ------------------------------------------------------------------

VEKTOR_PAGES = [
    {
        "url": "https://vektor-analytics.example",
        "text": (
            "Vektor Analytics builds a real-time data analytics platform used by "
            "e-commerce and fintech teams to turn event streams into live dashboards "
            "and alerts. Founded in Ankara in 2019, the team is remote-friendly and "
            "ships a Python/PostgreSQL backed platform processing billions of events "
            "per month."
        ),
    },
    {
        "url": "https://vektor-analytics.example/careers",
        "text": (
            "Careers at Vektor Analytics. Open positions: Backend Engineer (Python) - "
            "design and scale our ingestion APIs and stream processing services "
            "(FastAPI, PostgreSQL, Kafka). Data Engineer - own our Airflow pipelines. "
            "We are remote-friendly within Turkey and hold quarterly meetups in Ankara. "
            "Apply by email: careers@vektor-analytics.example"
        ),
    },
    {
        "url": "https://vektor-analytics.example/about",
        "text": (
            "About us. Vektor Analytics was founded by engineers from Ankara's startup "
            "ecosystem. Elif Demir leads the engineering team. We value evidence-driven "
            "product decisions and boring, reliable technology."
        ),
    },
]


def fake_fetch_company_research(website_url: str, careers_url: str | None = None) -> dict:
    return {"pages": VEKTOR_PAGES, "detected_careers_url": "https://vektor-analytics.example/careers"}


def fake_send_email(to, subject, body_text, attachment_path=None):
    return {"id": "demo-msg-001", "threadId": "demo-thread-001"}


def fake_send_reply(**kwargs):
    return {"id": "demo-msg-002", "threadId": kwargs.get("thread_id", "demo-thread-001")}


CHANNEL_JSON = {
    "summary": (
        "Vektor Analytics builds a real-time data analytics platform (dashboards and "
        "alerts over event streams) for e-commerce and fintech teams. Ankara-founded and "
        "remote-friendly, with open Backend Engineer (Python) and Data Engineer roles "
        "listed on the careers page."
    ),
    "channels": ["email"],
    "contact_email": "careers@vektor-analytics.example",
    "people": [{"name": "Elif Demir", "role": "Engineering Lead", "email": None}],
}

DRAFT_V1 = {
    "target_position": "Backend Engineer (Python)",
    "cv_choice_name": "Backend CV",
    "reasoning": (
        "1. Vektor Analytics builds a real-time analytics platform on a Python/PostgreSQL "
        "stack, with an open Backend Engineer (Python) role on its careers page. "
        "2. Your FastAPI services, the etl-pipeline project and your PostgreSQL/Airflow "
        "experience map directly onto their ingestion APIs and stream processing work. "
        "3. Recommendation: apply for Backend Engineer (Python) with the Backend CV - "
        "it is the closest match to the posted stack."
    ),
    "subject": "Application for Backend Engineer (Python) - Deniz Yilmaz",
    "cover_letter_body": (
        "Dear Vektor Analytics hiring team,\n\n"
        "I am writing to apply for the Backend Engineer (Python) position listed on your "
        "careers page. Your platform's focus on turning event streams into live dashboards, "
        "built on Python and PostgreSQL, matches the kind of backend work I enjoy most.\n\n"
        "Over the last three years I have built and operated FastAPI services and "
        "PostgreSQL-backed data pipelines, including an open-source ETL framework and an "
        "Airflow deployment handling daily batch and streaming loads. I care about boring, "
        "reliable systems - which your about page suggests your team does too.\n\n"
        "My CV is attached. I would be glad to talk about how I could contribute to your "
        "ingestion and stream processing services.\n\n"
        "Best regards,\nDeniz Yilmaz"
    ),
    "sources": [
        "https://vektor-analytics.example/careers",
        "https://vektor-analytics.example/about",
    ],
}

DRAFT_V2 = {
    "target_position": "Backend Engineer (Python)",
    "cv_choice_name": "Backend CV",
    "reasoning": (
        "1. Vektor Analytics runs a real-time analytics platform (Python/PostgreSQL/Kafka) "
        "and is hiring a Backend Engineer (Python). "
        "2. Revised per your feedback: shorter, and the etl-pipeline project is now front "
        "and center. "
        "3. Recommendation unchanged: Backend Engineer (Python) with the Backend CV."
    ),
    "subject": "Backend Engineer (Python) - Deniz Yilmaz",
    "cover_letter_body": (
        "Dear Vektor Analytics hiring team,\n\n"
        "I am applying for the Backend Engineer (Python) role on your careers page. "
        "I built etl-pipeline, an open-source Python ETL framework, and have three years "
        "of FastAPI + PostgreSQL production experience - a direct match for your ingestion "
        "APIs and stream processing stack.\n\n"
        "My CV is attached; I would welcome a conversation.\n\n"
        "Best regards,\nDeniz Yilmaz"
    ),
    "sources": ["https://vektor-analytics.example/careers"],
}


class DemoLLM(LLMProvider):
    """Keys off each caller's system prompt; returns deterministic canned JSON."""

    def generate(self, prompt, *, system=None, temperature=0.2, max_output_tokens=2048):
        s = system or ""
        if "conflicts with" in s:  # preference checker
            text = json.dumps({"flags": []})
        elif "do four things" in s:  # research channel/summary pass
            text = json.dumps(CHANNEL_JSON)
        elif "drafting a job application email" in s:  # draft / regenerate
            text = json.dumps(DRAFT_V2 if "OWNER FEEDBACK" in prompt else DRAFT_V1)
        else:
            text = "{}"
        return LLMResponse(text=text, model="demo", provider="demo")


# --- Patch the real modules -------------------------------------------------

import app.applications.review as review_mod  # noqa: E402
import app.applications.reply_check as reply_check_mod  # noqa: E402
import app.companies.workflow as workflow_mod  # noqa: E402
import app.email_send.gmail_client as gmail_mod  # noqa: E402

review_mod.send_email = fake_send_email
reply_check_mod.thread_has_reply = lambda thread_id: False
workflow_mod.fetch_company_research = fake_fetch_company_research
gmail_mod.send_email = fake_send_email
gmail_mod.send_reply = fake_send_reply

from app.main import app as fastapi_app  # noqa: E402
from app.web.deps import get_llm  # noqa: E402

fastapi_app.dependency_overrides[get_llm] = lambda: DemoLLM()

# --- Seed -------------------------------------------------------------------

MINIMAL_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"xref\n0 4\ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n0\n%%EOF\n"
)


def seed():
    now = datetime.now(timezone.utc)
    files_dir = os.path.join(BASE, "files")
    os.makedirs(files_dir, exist_ok=True)
    for fname in ("backend_cv.pdf", "ml_cv.pdf"):
        with open(os.path.join(files_dir, fname), "wb") as f:
            f.write(MINIMAL_PDF)

    db = SessionLocal()
    try:
        cv_backend = CV(
            name="Backend CV",
            target_role="Backend Engineer",
            file_path=os.path.join(files_dir, "backend_cv.pdf"),
            notes="Emphasizes FastAPI, PostgreSQL, data pipelines.",
        )
        cv_ml = CV(
            name="ML CV",
            target_role="ML Engineer",
            file_path=os.path.join(files_dir, "ml_cv.pdf"),
            notes="Emphasizes PyTorch, CV projects.",
        )
        db.add_all([cv_backend, cv_ml])

        db.add(
            Profile(
                summary=(
                    "Backend and data engineer with 3+ years of experience building "
                    "Python services (FastAPI, Django) and data pipelines (PostgreSQL, "
                    "Airflow, Kafka). Maintains several open-source projects, including "
                    "an ETL framework. Based in Ankara, open to remote roles."
                ),
                skills=[
                    {"skill": "Python", "evidence_source": "github.com/demo/etl-pipeline"},
                    {"skill": "FastAPI", "evidence_source": "github.com/demo/api-server"},
                    {"skill": "PostgreSQL", "evidence_source": "CV - Data Platform role"},
                    {"skill": "Airflow", "evidence_source": "CV - Data Platform role"},
                    {"skill": "Kafka", "evidence_source": "github.com/demo/stream-tools"},
                    {"skill": "Docker", "evidence_source": "github.com/demo/etl-pipeline"},
                ],
                target_roles=["Backend Engineer", "Data Engineer", "ML Engineer"],
            )
        )

        for ref, purpose in [
            ("demo/etl-pipeline", "Open-source Python ETL framework with pluggable sources."),
            ("demo/api-server", "FastAPI reference backend with auth and rate limiting."),
            ("demo/stream-tools", "Kafka stream processing utilities."),
            ("demo/cv-experiments", "Computer-vision training experiments (PyTorch)."),
        ]:
            db.add(
                ProfileItem(
                    source_type="github_repo",
                    source_ref=ref,
                    extracted={"purpose": purpose, "stack": ["Python"]},
                    status=ProfileItemStatus.ANALYZED,
                    source_urls=[f"https://github.com/{ref}"],
                )
            )
        db.add(
            ProfileItem(
                source_type="cv",
                source_ref="backend_cv.pdf",
                extracted={"purpose": "Backend-focused CV variant."},
                status=ProfileItemStatus.ANALYZED,
            )
        )
        db.add(
            ProfileItem(
                source_type="site",
                source_ref="https://deniz.example/blog",
                extracted={"purpose": "Personal blog - pipeline design posts."},
                status=ProfileItemStatus.NEEDS_REVIEW,
            )
        )

        db.add_all(
            [
                Preference(
                    description="No defense industry work",
                    type=PreferenceType.HARD,
                    signals=["defense", "military", "weapons"],
                ),
                Preference(
                    description="Prefer remote-friendly or hybrid (Ankara)",
                    type=PreferenceType.SOFT,
                    signals=["remote", "hybrid"],
                ),
                Preference(
                    description="Avoid pure outsourcing/consultancy shops",
                    type=PreferenceType.SOFT,
                    signals=["outsourcing", "bodyshopping"],
                ),
            ]
        )
        db.commit()

        # Blocked example: hard-exclusion flag with on-site evidence.
        aeronova = Company(
            name="Aeronova Systems",
            website_url="https://aeronova.example",
            contact_email="hr@aeronova.example",
            state=CompanyState.APPLICABLE,
            location="Ankara",
            technopark="Demo Teknokent",
            research={
                "pages": [
                    {
                        "url": "https://aeronova.example",
                        "text": "Aeronova Systems develops avionics software platforms.",
                    }
                ],
                "summary": (
                    "Aeronova Systems develops avionics and mission software. The site "
                    "highlights defense-platform integrations."
                ),
                "suggested_contacts": [],
                "fetched_at": now.isoformat(),
            },
            preference_check=[
                {
                    "preference_id": 1,
                    "matched": True,
                    "severity": "hard",
                    "evidence_snippet": (
                        "Aeronova delivers mission-critical avionics and fire-control "
                        "software for military aircraft platforms."
                    ),
                    "source_url": "https://aeronova.example/solutions",
                    "confidence": "high",
                }
            ],
        )

        cloudpeak = Company(
            name="CloudPeak Labs",
            website_url="https://cloudpeak.example",
            contact_email="jobs@cloudpeak.example",
            state=CompanyState.APPLICABLE,
            location="Ankara",
            research={
                "pages": [
                    {
                        "url": "https://cloudpeak.example",
                        "text": "CloudPeak Labs builds developer tooling for cloud cost optimization.",
                    }
                ],
                "summary": "CloudPeak Labs builds developer tooling for cloud cost optimization; hiring backend engineers.",
                "suggested_contacts": [],
                "fetched_at": now.isoformat(),
            },
        )

        norda = Company(
            name="Norda Robotics",
            website_url="https://norda-robotics.example",
            state=CompanyState.APPLICABLE,
            location="Ankara",
            research={},
        )

        pixelworks = Company(
            name="PixelWorks Studio",
            website_url="https://pixelworks.example",
            state=CompanyState.ON_HOLD,
            location="Istanbul",
            research={},
            notes="Mostly outsourcing work - on hold for now.",
        )

        dataforge = Company(
            name="DataForge Technologies",
            website_url="https://dataforge.example",
            contact_email="jobs@dataforge.example",
            state=CompanyState.APPLICABLE,
            location="Ankara",
            technopark="Demo Teknokent",
            research={
                "pages": [
                    {
                        "url": "https://dataforge.example",
                        "text": "DataForge Technologies provides managed data warehouse services.",
                    }
                ],
                "summary": "DataForge Technologies provides managed data warehouse services.",
                "suggested_contacts": [],
                "fetched_at": (now - timedelta(days=21)).isoformat(),
            },
        )

        brightstack = Company(
            name="BrightStack",
            website_url="https://brightstack.example",
            contact_email="careers@brightstack.example",
            state=CompanyState.APPLICABLE,
            location="Remote",
            research={
                "pages": [
                    {
                        "url": "https://brightstack.example",
                        "text": "BrightStack builds e-commerce infrastructure APIs.",
                    }
                ],
                "summary": "BrightStack builds e-commerce infrastructure APIs.",
                "suggested_contacts": [],
                "fetched_at": (now - timedelta(days=32)).isoformat(),
            },
        )

        db.add_all([aeronova, cloudpeak, norda, pixelworks, dataforge, brightstack])
        db.commit()

        sent_df = now - timedelta(days=20)
        db.add(
            Application(
                company_id=dataforge.id,
                cv_id=cv_backend.id,
                target_position="Data Engineer",
                subject="Application for Data Engineer - Deniz Yilmaz",
                cover_letter_body="(sent 20 days ago - awaiting reply)",
                sources=["https://dataforge.example/careers"],
                state=ApplicationState.AWAITING_REPLY,
                sent_at=sent_df,
                follow_up_due_at=sent_df + timedelta(days=14),
                gmail_message_id="demo-msg-df",
                gmail_thread_id="demo-thread-df",
                last_checked_at=now,
                revision_history=[{"reasoning": "Data Engineer role matched Airflow experience."}],
            )
        )
        db.add(
            Contact(
                company_id=dataforge.id,
                name="Mert Aksoy",
                role="Backend Team Lead",
                email="mert.aksoy@dataforge.example",
                source_url="https://dataforge.example/team",
                added_manually=True,
            )
        )

        sent_bs = now - timedelta(days=32)
        db.add(
            Application(
                company_id=brightstack.id,
                cv_id=cv_backend.id,
                target_position="Backend Engineer",
                subject="Application for Backend Engineer - Deniz Yilmaz",
                cover_letter_body="(sent 32 days ago - moved to interview)",
                sources=["https://brightstack.example/careers"],
                state=ApplicationState.INTERVIEW,
                sent_at=sent_bs,
                follow_up_due_at=sent_bs + timedelta(days=14),
                gmail_message_id="demo-msg-bs",
                gmail_thread_id="demo-thread-bs",
                last_checked_at=now,
                revision_history=[{"reasoning": "Backend role; e-commerce APIs."}],
            )
        )
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    init_db()
    seed()
    import uvicorn

    uvicorn.run(fastapi_app, host="127.0.0.1", port=8123, log_level="warning")
