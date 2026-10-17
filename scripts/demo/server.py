"""Demo server for the README GIF.

Runs the real app UI against an isolated demo database, with:
- a canned-response LLM provider (no API key, no network),
- a fake Gmail sender (nothing is ever actually sent),
- a fake web fetcher and a fake technopark directory (no requests to real sites),
so every flow can be clicked through live and deterministically. All people,
companies and addresses are fictional (*.example).
"""
import json
import os
import re
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
os.environ["LINKEDIN_RESEARCH_ENABLED"] = "false"

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

# --- Fake company website ------------------------------------------------------

VEKTOR_PAGES = [
    {
        "url": "https://vektor-analytics.example",
        "text": (
            "Vektor Analytics builds a real-time analytics platform that turns e-commerce "
            "and payment event streams into live dashboards and alerts. Founded in Ankara in "
            "2019, the team is remote-friendly and runs a Python and PostgreSQL platform that "
            "processes billions of events a month for online retailers."
        ),
    },
    {
        "url": "https://vektor-analytics.example/careers",
        "text": (
            "Careers at Vektor Analytics. Open positions: Backend Engineer (Python) - build and "
            "scale the ingestion APIs and stream processing services (FastAPI, PostgreSQL, "
            "Kafka). Data Engineer - own our Airflow pipelines. Remote-friendly within Turkey, "
            "quarterly meetups in Ankara. Apply by email: careers@vektor-analytics.example"
        ),
    },
    {
        "url": "https://vektor-analytics.example/about",
        "text": (
            "About us. Vektor Analytics was founded by engineers from Ankara's startup scene. "
            "Elif Demir leads the engineering team. We like evidence-driven product decisions "
            "and boring, reliable technology."
        ),
    },
]


def fake_fetch_company_research(website_url: str, careers_url: str | None = None) -> dict:
    return {
        "pages": VEKTOR_PAGES,
        "detected_careers_url": "https://vektor-analytics.example/careers",
        "careers_page_thin": False,
        "detected_linkedin_url": None,
        "content_chars": sum(len(p["text"]) for p in VEKTOR_PAGES),
    }


def fake_send_email(to, subject, body_text, attachment_path=None, attachment_name=None):
    return {"id": "demo-msg-001", "threadId": "demo-thread-001"}


def fake_send_reply(**kwargs):
    return {"id": "demo-msg-002", "threadId": kwargs.get("thread_id", "demo-thread-001")}


# --- Fake technopark directory -------------------------------------------------

DEMO_SOURCE = {"name": "Demo Teknokent", "city": "Ankara", "url": "https://teknokent.example/firmalar", "kind": "html"}

DIRECTORY = [
    {"name": "ORBİS VERİ YAZILIM A.Ş.", "website_url": "https://orbis-data.example", "sector": "Yazılım"},
    {"name": "KUZEY GÖRÜNTÜ TEKNOLOJİLERİ LTD. ŞTİ.", "website_url": "https://kuzey-vision.example", "sector": "Yazılım"},
    {"name": "ATLAS SAVUNMA ELEKTRONİK A.Ş.", "website_url": "https://atlas-defence.example", "sector": "Savunma Sanayi"},
    {"name": "MAVİ BİLGİ DANIŞMANLIK LTD. ŞTİ.", "website_url": "https://mavi-consulting.example", "sector": "Danışmanlık"},
    {"name": "PERA SENSÖR SİSTEMLERİ A.Ş.", "website_url": "https://pera-sensor.example", "sector": "Elektronik"},
    {"name": "LUMEN BİLİŞİM TEKNOLOJİLERİ A.Ş.", "website_url": None, "sector": "Yazılım"},
]


def fake_read_source(llm, kind, url):
    return [{**entry, "contact_email": None, "brand_name": None} for entry in DIRECTORY]


HOMEPAGES = {
    "orbis-data.example": (
        "Orbis builds a data platform for logistics companies: it collects shipment events, "
        "cleans them in streaming pipelines written in Python, and serves delivery-time "
        "forecasts through an API. Our engineering team in Ankara works with FastAPI, "
        "PostgreSQL and Kafka. Careers: we are hiring backend and data engineers."
    ),
    "kuzey-vision.example": (
        "Kuzey develops camera-based quality inspection software for factories. Our models "
        "find defects on production lines in real time; the platform is written in Python "
        "with PyTorch and runs on edge devices. Join our team - see open roles."
    ),
    "atlas-defence.example": (
        "Atlas Savunma Elektronik designs fire-control electronics and mission computers for "
        "military land vehicles, delivered to defence integrators. Our engineers work on "
        "embedded hardware and real-time firmware."
    ),
    "mavi-consulting.example": (
        "Mavi Bilgi provides IT outsourcing and staff augmentation: our consultants join "
        "client projects in banking and telecom on a time-and-materials basis. Java, .NET "
        "and some Python work."
    ),
    "pera-sensor.example": (
        "Pera designs and manufactures industrial humidity and temperature sensors and the "
        "electronics that read them. Hardware design, PCB production and calibration."
    ),
}

TRIAGE = {
    "orbis-data.example": {
        "brand_name": "Orbis",
        "summary": "Builds a data platform that turns logistics shipment events into delivery-time forecasts.",
        "sector": "data platform / logistics software",
        "fit": "high",
        "fit_reason": "Python streaming pipelines and APIs - core backend and data engineering work.",
        "flags": [],
    },
    "kuzey-vision.example": {
        "brand_name": "Kuzey Vision",
        "summary": "Makes camera-based software that finds defects on factory production lines.",
        "sector": "computer vision software",
        "fit": "high",
        "fit_reason": "Python and PyTorch computer vision work, close to the ML target role.",
        "flags": [],
    },
    "atlas-defence.example": {
        "brand_name": "Atlas Savunma",
        "summary": "Designs fire-control electronics and mission computers for military vehicles.",
        "sector": "defence electronics",
        "fit": "low",
        "fit_reason": "Mostly embedded hardware and firmware.",
        "flags": [{"preference_id": 1, "evidence_snippet": "fire-control electronics and mission computers for military land vehicles"}],
    },
    "mavi-consulting.example": {
        "brand_name": "Mavi Bilgi",
        "summary": "Places consultants on client banking and telecom projects.",
        "sector": "IT outsourcing",
        "fit": "medium",
        "fit_reason": "Some Python work, but as outsourced staff on client projects.",
        "flags": [{"preference_id": 3, "evidence_snippet": "IT outsourcing and staff augmentation"}],
    },
    "pera-sensor.example": {
        "brand_name": "Pera Sensör",
        "summary": "Designs and manufactures industrial humidity and temperature sensors.",
        "sector": "sensor hardware",
        "fit": "low",
        "fit_reason": "Hardware design and manufacturing, not software.",
        "flags": [],
    },
}


def fake_fetch_homepage(url):
    host = re.sub(r"^https?://", "", url).split("/")[0]
    text = HOMEPAGES.get(host, "")
    html = "<html><body><a href='/careers'>Careers</a></body></html>" if "Careers" in text or "careers" in text.lower() else "<html></html>"
    return html, text * 2  # comfortably above the "readable text" minimum


# --- Canned LLM ----------------------------------------------------------------

RESEARCH_JSON = {
    "summary": (
        "Vektor Analytics builds a real-time analytics platform that turns e-commerce and payment "
        "event streams into live dashboards and alerts. Ankara-founded and remote-friendly; "
        "Python, PostgreSQL and Kafka stack."
    ),
    "open_positions": [
        {"title": "Backend Engineer (Python)", "details": "Ingestion APIs and stream processing - FastAPI, PostgreSQL, Kafka."},
        {"title": "Data Engineer", "details": "Own the Airflow pipelines."},
    ],
    "careers": {"has_careers_page": True, "application_form": False, "external_portal": None, "notes": "Applications by email."},
    "channels": ["email"],
    "emails": [{"email": "careers@vektor-analytics.example", "context": "apply by email"}],
    "people": [{"name": "Elif Demir", "role": "Engineering lead", "email": None}],
    "fit": {
        "recommended_position": "Backend Engineer (Python)",
        "reasoning": "Their ingestion APIs run on FastAPI and PostgreSQL - the same stack as your api-server and etl-pipeline projects.",
    },
}

LETTER_V1 = """Merhaba Vektor Analytics ekibi,

Ankara'da yaşayan bir backend geliştiricisiyim ve kariyer sayfanızdaki Backend Engineer (Python) pozisyonu için yazıyorum.

Sitenizde, e-ticaret ve ödeme sistemlerinden gelen olay akışlarını canlı panellere ve uyarılara dönüştüren bir analiz platformu geliştirdiğinizi okudum. İlandaki işin büyük kısmı, bu olayları alan API'ler ve akış işleme servisleri; benim son üç yılda en çok uğraştığım şey de tam olarak bu.

GitHub'daki etl-pipeline projemde, farklı kaynaklardan gelen verileri Python ile toplayıp temizleyen ve PostgreSQL'e yükleyen bir ETL çatısı yazdım. Kaynakları eklenti olarak tanımlanabilir yaptım; böylece yeni bir veri kaynağı eklemek birkaç satırlık bir iş haline geldi. Projeyi Docker ile paketledim ve testlerini GitHub Actions üzerinde çalıştırıyorum.

api-server projemde ise FastAPI ile kimlik doğrulama ve istek sınırlama içeren bir referans backend kurdum. Burada özellikle yüksek trafik altında veritabanı bağlantılarını nasıl yöneteceğimi ve hataları nasıl görünür kılacağımı öğrendim. stream-tools ile de Kafka üzerinde küçük akış işleme araçları geliştirdim; bu, ilandaki Kafka kısmına en yakın deneyimim.

Önceki işimde bir veri platformu ekibinde Airflow üzerinde günlük ve saatlik veri yüklerini yöneten iş akışları yazdım. Sessizce bozulan bir işi fark etmenin, hızlı ama güvenilmez bir çözümden daha değerli olduğunu orada gördüm. Sitenizdeki "sıkıcı ama güvenilir teknoloji" vurgusu bu yüzden dikkatimi çekti.

CV'mi ekte gönderiyorum. Uygun görürseniz ingestion tarafındaki işleri ve ekibinizin çalışma şeklini konuşmaktan memnun olurum.

İyi çalışmalar,
Deniz Yılmaz"""

LETTER_V2 = """Merhaba Vektor Analytics ekibi,

Kariyer sayfanızdaki Backend Engineer (Python) pozisyonu için yazıyorum.

Sitenizde, e-ticaret ve ödeme olaylarını canlı panellere ve uyarılara dönüştüren bir platform geliştirdiğinizi gördüm. İlandaki ingestion API'leri ve akış işleme servisleri, benim de en çok çalıştığım alan.

En yakın işim etl-pipeline: farklı kaynaklardan gelen verileri Python ile toplayan, temizleyen ve PostgreSQL'e yükleyen açık kaynak bir ETL çatısı. Kaynakları eklenti olarak tasarladım, böylece yeni bir kaynak eklemek birkaç satırlık bir iş oldu. Projeyi Docker ile paketledim, testlerini GitHub Actions'ta çalıştırıyorum ve şu an günlük milyonlarca satırı işleyen bir kurulumda kullanılıyor.

Bunun yanında FastAPI ile kimlik doğrulama ve istek sınırlama içeren bir backend (api-server) kurdum ve Kafka üzerinde küçük akış işleme araçları (stream-tools) yazdım. Önceki işimde de bir veri platformu ekibinde Airflow iş akışları geliştirdim; burada sessizce bozulan bir işi zamanında fark etmenin ne kadar önemli olduğunu öğrendim.

Sitenizdeki "sıkıcı ama güvenilir teknoloji" yaklaşımı, kendi çalışma şeklime de uyuyor.

CV'mi ekte gönderiyorum. Uygun görürseniz ingestion tarafındaki işleri konuşmaktan memnun olurum.

İyi çalışmalar,
Deniz Yılmaz"""


def _draft(body: str, reasoning: str) -> dict:
    return {
        "target_position": "Backend Engineer (Python)",
        "cv_choice_name": "Backend CV",
        "reasoning": reasoning,
        "subject": "Junior Backend Engineer (Python) Başvurusu",
        "cover_letter_body": body,
        "sources": ["https://vektor-analytics.example", "https://vektor-analytics.example/careers"],
        "evidence_item_ids": [1, 2, 3],
        "unsupported_claims": [],
    }


DRAFT_V1 = _draft(
    LETTER_V1,
    "(1) Vektor Analytics turns e-commerce and payment event streams into live dashboards, on a "
    "Python, PostgreSQL and Kafka stack. (2) Referenced: etl-pipeline (Python ETL into PostgreSQL), "
    "api-server (FastAPI) and stream-tools (Kafka) - each matches a part of the posted role. "
    "(3) Backend Engineer (Python) with the Backend CV - closest to the posted stack.",
)
DRAFT_V2 = _draft(
    LETTER_V2,
    "(1) Same company facts as before. (2) Per your feedback: shorter, and etl-pipeline now leads "
    "as the closest project. (3) Unchanged: Backend Engineer (Python) with the Backend CV.",
)


class DemoLLM(LLMProvider):
    """Keys off each caller's system prompt; returns deterministic canned JSON."""

    def generate(self, prompt, *, system=None, temperature=0.2, max_output_tokens=2048):
        s = system or ""
        if "conflicts with" in s:  # preference check during research
            data = {"flags": []}
        elif "researching a company's website" in s:
            data = RESEARCH_JSON
        elif "writing a job application email" in s:  # draft / regenerate
            data = DRAFT_V2 if "OWNER FEEDBACK" in prompt else DRAFT_V1
        elif "screening a company" in s:  # discovery homepage screening
            host = next((h for h in TRIAGE if h in prompt), None)
            data = TRIAGE.get(host, {"summary": "", "fit": "low", "flags": []})
        elif "short follow-up email" in s:
            data = {"body": "Merhaba,\n\nBirkaç hafta önce Data Engineer pozisyonu için yazmıştım. Başvurumun durumunu öğrenebilir miyim?\n\nİyi çalışmalar,\nDeniz Yılmaz"}
        else:
            data = {}
        return LLMResponse(text=json.dumps(data, ensure_ascii=False), model="demo", provider="demo")


# --- Patch the real modules -------------------------------------------------

import app.applications.reply_check as reply_check_mod  # noqa: E402
import app.applications.review as review_mod  # noqa: E402
import app.companies.workflow as workflow_mod  # noqa: E402
import app.discovery.service as discovery_service  # noqa: E402
import app.discovery.triage as triage_mod  # noqa: E402
import app.email_send.gmail_client as gmail_mod  # noqa: E402

review_mod.send_email = fake_send_email
reply_check_mod.thread_has_reply = lambda thread_id: False
workflow_mod.fetch_company_research = fake_fetch_company_research
gmail_mod.send_email = fake_send_email
gmail_mod.send_reply = fake_send_reply
discovery_service.DEFAULT_SOURCES = [DEMO_SOURCE]
discovery_service.read_source = fake_read_source
triage_mod._fetch_homepage = fake_fetch_homepage

from app.main import app as fastapi_app  # noqa: E402
from app.web.deps import get_draft_llm, get_llm  # noqa: E402

fastapi_app.dependency_overrides[get_llm] = lambda: DemoLLM()
fastapi_app.dependency_overrides[get_draft_llm] = lambda: DemoLLM()

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
            notes="FastAPI, PostgreSQL, data pipelines.",
        )
        cv_ml = CV(
            name="ML CV",
            target_role="ML Engineer",
            file_path=os.path.join(files_dir, "ml_cv.pdf"),
            notes="PyTorch, computer vision projects.",
        )
        db.add_all([cv_backend, cv_ml])

        db.add(
            Profile(
                summary=(
                    "Backend and data engineer with 3 years of experience building Python services "
                    "(FastAPI) and data pipelines (PostgreSQL, Airflow, Kafka). Maintains open-source "
                    "projects including an ETL framework. Based in Ankara, open to remote roles."
                ),
                skills=[
                    {"skill": "Python", "evidence_source": "demo/etl-pipeline"},
                    {"skill": "FastAPI", "evidence_source": "demo/api-server"},
                    {"skill": "PostgreSQL", "evidence_source": "backend_cv.pdf"},
                    {"skill": "Airflow", "evidence_source": "backend_cv.pdf"},
                    {"skill": "Kafka", "evidence_source": "demo/stream-tools"},
                    {"skill": "Docker", "evidence_source": "demo/etl-pipeline"},
                    {"skill": "PyTorch", "evidence_source": "demo/defect-detector"},
                ],
                target_roles=["Backend Engineer", "Data Engineer", "ML Engineer"],
            )
        )

        # ids 1-4: repos, 5: CV, 6: LinkedIn, 7: a site that still needs review
        for ref, purpose, stack in [
            ("demo/etl-pipeline", "Open-source Python ETL framework with pluggable sources, loading into PostgreSQL.", ["Python", "PostgreSQL", "Docker"]),
            ("demo/api-server", "FastAPI reference backend with authentication and rate limiting.", ["Python", "FastAPI"]),
            ("demo/stream-tools", "Small Kafka stream processing utilities.", ["Python", "Kafka"]),
            ("demo/defect-detector", "PyTorch model that finds surface defects in product photos.", ["Python", "PyTorch"]),
        ]:
            db.add(
                ProfileItem(
                    source_type="github_repo",
                    source_ref=ref,
                    extracted={"purpose": purpose, "stack": stack, "role": "author", "highlights": []},
                    status=ProfileItemStatus.ANALYZED,
                    source_urls=[f"https://github.com/{ref}"],
                )
            )
        db.add(
            ProfileItem(
                source_type="cv",
                source_ref="backend_cv.pdf",
                extracted={"purpose": "Backend-focused CV: data platform team, Airflow pipelines.", "stack": ["Python", "Airflow"], "role": "Backend developer", "highlights": []},
                status=ProfileItemStatus.ANALYZED,
            )
        )
        db.add(
            ProfileItem(
                source_type="linkedin",
                source_ref="linkedin_export.pdf",
                extracted={"purpose": "LinkedIn profile export.", "stack": ["Python"], "role": "Backend developer", "highlights": []},
                status=ProfileItemStatus.ANALYZED,
            )
        )
        db.add(
            ProfileItem(
                source_type="site",
                source_ref="https://deniz.example/blog",
                extracted={"purpose": "unknown", "stack": [], "role": "unknown", "highlights": []},
                status=ProfileItemStatus.NEEDS_REVIEW,
            )
        )

        db.add_all(
            [
                Preference(description="No defence industry work", type=PreferenceType.HARD, signals=["defence", "military"]),
                Preference(description="Prefer remote-friendly or hybrid (Ankara)", type=PreferenceType.SOFT, signals=["remote", "hybrid"]),
                Preference(description="Avoid pure outsourcing shops", type=PreferenceType.SOFT, signals=["outsourcing"]),
            ]
        )
        db.commit()

        # Blocked example: a hard-exclusion match with on-site evidence.
        aeronova = Company(
            name="Aeronova Systems",
            website_url="https://aeronova.example",
            contact_email="hr@aeronova.example",
            state=CompanyState.APPLICABLE,
            location="Ankara",
            technopark="Demo Teknokent",
            research={
                "pages": [{"url": "https://aeronova.example", "text": "Aeronova Systems develops avionics software platforms for aircraft programs. " * 4}],
                "summary": "Aeronova Systems develops avionics and mission software; the site highlights military aircraft programs.",
                "suggested_contacts": [],
                "content_chars": 400,
                "fetched_at": now.isoformat(),
            },
            preference_check=[
                {
                    "preference_id": 1,
                    "matched": True,
                    "severity": "hard",
                    "evidence_snippet": "mission-critical avionics and fire-control software for military aircraft platforms",
                    "source_url": "https://aeronova.example/solutions",
                    "confidence": "high",
                }
            ],
        )
        dataforge = Company(
            name="DataForge",
            website_url="https://dataforge.example",
            contact_email="jobs@dataforge.example",
            state=CompanyState.APPLICABLE,
            location="Ankara",
            technopark="Demo Teknokent",
            research={
                "pages": [{"url": "https://dataforge.example", "text": "DataForge provides managed data warehouse services. " * 6}],
                "summary": "DataForge provides managed data warehouse services.",
                "suggested_contacts": [],
                "content_chars": 400,
                "fetched_at": (now - timedelta(days=21)).isoformat(),
            },
        )
        brightstack = Company(
            name="BrightStack",
            website_url="https://brightstack.example",
            contact_email="careers@brightstack.example",
            state=CompanyState.APPLICABLE,
            location="Ankara",
            research={
                "pages": [{"url": "https://brightstack.example", "text": "BrightStack builds e-commerce infrastructure APIs. " * 6}],
                "summary": "BrightStack builds e-commerce infrastructure APIs.",
                "suggested_contacts": [],
                "content_chars": 400,
                "fetched_at": (now - timedelta(days=32)).isoformat(),
            },
        )
        norda = Company(name="Norda Robotics", website_url="https://norda-robotics.example", state=CompanyState.APPLICABLE, location="Ankara", research={})
        db.add_all([aeronova, dataforge, brightstack, norda])
        db.commit()

        sent_df = now - timedelta(days=20)
        db.add(
            Application(
                company_id=dataforge.id,
                cv_id=cv_backend.id,
                target_position="Data Engineer",
                subject="Junior Data Engineer Başvurusu",
                cover_letter_body="(sent 20 days ago - no reply yet)",
                sources=["https://dataforge.example"],
                state=ApplicationState.AWAITING_REPLY,
                sent_at=sent_df,
                follow_up_due_at=sent_df + timedelta(days=14),
                gmail_message_id="demo-msg-df",
                gmail_thread_id="demo-thread-df",
                last_checked_at=now,
                revision_history=[{"reasoning": "Data Engineer role matched the Airflow experience."}],
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
                subject="Junior Backend Engineer Başvurusu",
                cover_letter_body="(sent 32 days ago - moved to interview)",
                sources=["https://brightstack.example"],
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
