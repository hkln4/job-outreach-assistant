"""Regenerate the README screenshots (docs/screenshots/*.png).

Seeds a throwaway database with fictional data (made-up people and
companies, *.example addresses), starts the app on it, and captures each
page with headless Chrome over the DevTools protocol. No API key, no real
site, nothing is sent; the owner's data/ folder and .env are never read.

    python scripts/screenshots.py            # Chrome at the usual macOS path
    CHROME=/path/to/chrome python scripts/screenshots.py
"""
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"
WORK = Path(tempfile.mkdtemp(prefix="outreach-shots-"))
PORT = 8777
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

ENV = {
    "DATABASE_URL": f"sqlite:///{WORK}/shots.db",
    "ALLOWED_HOSTS": "127.0.0.1,localhost",
    "GENERATED_CVS_DIR": str(WORK), "CV_VARIANTS_DIR": str(WORK), "SENT_CVS_DIR": str(WORK), "DISCOVERY_UPLOADS_DIR": str(WORK),
    "LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "not-real", "ANTHROPIC_API_KEY": "not-real",
    "LINKEDIN_RESEARCH_ENABLED": "false", "SEARCH_API_ENABLED": "false", "DRAFT_REVIEW_ENABLED": "false",
}
os.environ.update(ENV)
sys.path.insert(0, str(ROOT))

NOW = datetime.now(timezone.utc)


def ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


def seed() -> None:
    from pypdf import PdfWriter

    from app.db import SessionLocal, init_db
    from app.models import (
        CV, Application, ApplicationState, AtsBoard, Bullet, CandidateStatus, Company, CompanyCandidate, CompanyState,
        CVProject, DiscoverySource, JobPosting, LLMCallLog, OwnerSettings, PostingStatus, Preference, PreferenceType,
        Profile, ProfileItem, ProfileItemStatus, StyleNote, TechSkill,
    )

    init_db()
    db = SessionLocal()
    cv_path = WORK / "Ada_Demir_CV.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with open(cv_path, "wb") as fh:
        writer.write(fh)
    cv = CV(name="ML / CV Engineer CV", target_role="ML Engineer", file_path=str(cv_path))
    db.add(cv)
    db.commit()

    db.add(OwnerSettings(full_name="Ada Demir", title="Computer Engineer", email="ada.demir@example.com", phone="+90 555 000 00 00",
                         city="Ankara", github_url="https://github.com/ada-demo", linkedin_url="https://linkedin.com/in/ada-demo",
                         base_cv_id=cv.id, cv_template_approved=True,
                         posting_filter={"places": [{"place": "Ankara", "modes": ["onsite", "hybrid", "remote"]},
                                                    {"place": "Istanbul", "modes": ["hybrid", "remote"]}]}))
    db.add(Profile(
        summary="Computer engineering graduate working on computer vision and ML systems, from data pipelines to deployed models.",
        target_roles=["ML Engineer", "Computer Vision Engineer", "Data Scientist"],
        skills=[{"skill": "Python", "evidence_source": "E1"}, {"skill": "PyTorch", "evidence_source": "E1"}],
        writeup=(
            "# Summary\nComputer engineering graduate (Ankara University, 2026) focused on computer vision and applied ML. "
            "Builds the whole path: data preparation, training, evaluation and a small service around the model.\n\n"
            "# Projects\n## echo-ef - left-ventricle segmentation\nA U-Net that segments the left ventricle in echocardiography "
            "video and estimates ejection fraction; Dice 0.931 on the CAMUS test split [E1]. Solo project; trained on 1,800 "
            "annotated frames.\n## rag-qa - question answering over documents\nRetrieval-augmented QA over a set of PDF manuals "
            "with LangChain and FAISS, served with FastAPI [E2].\n## fleet-api - route planning service\nA FastAPI service "
            "that plans delivery routes with OR-Tools; PostgreSQL and Docker [E3].\n\n"
            "# Experience\n## Data Science Intern - Example Games (07/2025 - 09/2025)\n- Processed 1.5M+ user records from BigQuery.\n"
            "- Built a two-stage LTV model with LightGBM [E4].\n\n# Education\nB.Sc. Computer Engineering, Ankara University, GPA 3.58 / 4.00."
        ),
    ))
    items = [
        ProfileItem(source_type="github_repo", source_ref="ada-demo/echo-ef", status=ProfileItemStatus.ANALYZED,
                    source_urls=["https://github.com/ada-demo/echo-ef"],
                    extracted={"one_liner": "Echocardiography left-ventricle segmentation, Dice 0.931 on CAMUS",
                               "results": [{"claim": "Dice 0.931 on CAMUS", "quote": "Dice 0.931", "source": "readme"}],
                               "open_questions": ["Did you run this on real hospital data, or only on CAMUS?"]},
                    facts={"stack": ["Python", "PyTorch", "OpenCV", "FastAPI"], "activity": {"stars": 14, "commits": 212}},
                    source_version={"pushed_at": ago(20).isoformat()}),
        ProfileItem(source_type="github_repo", source_ref="ada-demo/rag-qa", status=ProfileItemStatus.ANALYZED,
                    extracted={"one_liner": "Question answering over PDF manuals with retrieval-augmented generation",
                               "open_questions": ["How many documents does it index, and how fast is an answer?"]},
                    facts={"stack": ["Python", "LangChain", "FAISS", "FastAPI"], "activity": {"stars": 3, "commits": 64}}),
        ProfileItem(source_type="github_repo", source_ref="ada-demo/fleet-api", status=ProfileItemStatus.ANALYZED,
                    extracted={"one_liner": "Delivery route planning API with OR-Tools"},
                    facts={"stack": ["Python", "FastAPI", "PostgreSQL", "Docker"], "activity": {"commits": 88}}),
        ProfileItem(source_type="cv", source_ref=f"{cv_path}#1", parent_ref=str(cv_path), status=ProfileItemStatus.ANALYZED,
                    extracted={"entry_type": "experience", "title": "Data Science Intern", "role": "Data Science Intern",
                               "organization": "Example Games", "start": "07/2025", "end": "09/2025", "location": "Ankara",
                               "bullets": ["Processed 1.5M+ user records from BigQuery on Google Cloud.",
                                           "Built a two-stage LTV prediction model with LightGBM, improving mid-segment accuracy by 4%."]}),
        ProfileItem(source_type="cv", source_ref=f"{cv_path}#2", parent_ref=str(cv_path), status=ProfileItemStatus.ANALYZED,
                    extracted={"entry_type": "education", "title": "B.Sc. Computer Engineering", "role": "B.Sc. Computer Engineering",
                               "organization": "Ankara University", "start": "09/2022", "end": "06/2026", "location": "Ankara",
                               "bullets": ["GPA: 3.58 / 4.00"]}),
        ProfileItem(source_type="site", source_ref="https://ada-demo.example/projects", status=ProfileItemStatus.ANALYZED,
                    extracted={"one_liner": "Portfolio: project write-ups and a short CV"}),
    ]
    db.add_all(items)
    db.commit()
    echo, rag, fleet = items[0], items[1], items[2]

    projects = [
        (echo, "echo-ef", "high", 40, [
            ("Built a U-Net pipeline that segments the left ventricle in echocardiography video, reaching Dice 0.931 on CAMUS.", "approved"),
            ("Estimated ejection fraction from the segmentation and served it through a FastAPI endpoint.", "approved"),
            ("Prepared 1,800 annotated frames with OpenCV augmentation for training and evaluation.", "draft"),
        ]),
        (rag, "rag-qa", "normal", 22, [
            ("Developed retrieval-augmented question answering over PDF manuals with LangChain and FAISS.", "approved"),
            ("Served answers with sources through a FastAPI service.", "draft"),
        ]),
        (fleet, "fleet-api", "low", 15, [("Designed a route planning API with OR-Tools, PostgreSQL and Docker.", "approved")]),
    ]
    for rank, (item, name, priority, score, bullets) in enumerate(projects):
        project = CVProject(profile_item_id=item.id, name=name, selected=True, priority=priority, score=score, sort_order=rank,
                            link_url=f"https://github.com/ada-demo/{name}")
        db.add(project)
        db.commit()
        for order, (text, status) in enumerate(bullets):
            db.add(Bullet(project_id=project.id, text=text, status=status, sort_order=order,
                          flags={"ats": [], "needs_metric": False, "ungrounded_numbers": [], "ungrounded_tech": []},
                          source={"quote": text.split(",")[0][:60], "kind": "record"}))
    for name, group, tier, refs in [
        ("Python", "languages", "core", 4), ("SQL", "languages", "core", 2), ("PyTorch", "frameworks", "core", 2),
        ("FastAPI", "frameworks", "core", 3), ("OpenCV", "frameworks", "core", 1), ("Docker", "tools", "core", 1),
        ("LangChain", "generative_ai", "specific", 1), ("FAISS", "generative_ai", "specific", 1), ("OR-Tools", "tools", "specific", 1),
        ("Computer Vision", "technologies", "core", 2), ("Machine Learning", "technologies", "core", 3),
    ]:
        db.add(TechSkill(name=name, group=group, tier=tier, evidence=[{"kind": "github_repo", "ref": f"repo{i}"} for i in range(refs)]))

    db.add_all([
        Preference(description="Avoid the defense industry", type=PreferenceType.HARD, signals=["savunma", "defense", "military"]),
        Preference(description="Prefer product companies over pure consultancy", type=PreferenceType.SOFT, signals=["consultancy", "danışmanlık"]),
    ])
    db.add(StyleNote(text="Keep it under 170 words; no \"I am writing to apply\" opening.", active=True))

    def research(summary: str, page_text: str, url: str) -> dict:
        page_text = page_text + " " + " ".join(
            ["Our engineers work in Ankara with Python and modern ML tooling.", "We ship to customers across Turkey and Europe.",
             "Open roles are listed on our careers page.", "We value clean code, measurement and small teams."] * 2)
        return {"pages": [{"url": url, "kind": "home", "text": page_text}], "content_chars": len(page_text), "summary": summary,
                "fetched_at": ago(2).isoformat(), "summary_verified": [{"sentence": summary, "quote": page_text[:60], "url": url}],
                "open_positions": [{"title": "Junior Computer Vision Engineer", "url": url + "careers", "verified": True}]}

    companies = [
        ("Görüntü Analitik", "https://goruntu.example/", "ODTÜ Teknokent", CompanyState.APPLICABLE, "ik@goruntu.example",
         "Builds visual inspection systems for textile and automotive factories.",
         "We build defect detection for textile and automotive production lines using deep learning."),
        ("Veridata Labs", "https://veridata.example/", "Bilkent Cyberpark", CompanyState.APPLICABLE, "careers@veridata.example",
         "Data platform and forecasting for retail chains.", "Our data platform helps retail chains forecast demand and plan stock."),
        ("Rota Lojistik Yazılım", "https://rota.example/", "Hacettepe Teknokent", CompanyState.APPLICABLE, "hr@rota.example",
         "Route optimization software for logistics fleets.", "Route optimization software for logistics fleets across Turkey."),
        ("Medvizyon", "https://medvizyon.example/", "ODTÜ Teknokent", CompanyState.APPLICABLE, "kariyer@medvizyon.example",
         "Medical imaging software for radiology departments.", "Medical imaging AI that helps radiologists read scans faster."),
        ("Anka Savunma Sistemleri", "https://anka-savunma.example/", "OSTİM Teknopark", CompanyState.ON_HOLD, "info@anka-savunma.example",
         "Embedded systems for defense platforms.", "Savunma sanayi için gömülü sistemler ve görüntü işleme."),
    ]
    rows = []
    for name, site, park, state, email, summary, text in companies:
        company = Company(name=name, website_url=site, technopark=park, location="Ankara", state=state, contact_email=email,
                          careers_url=site + "careers", research=research(summary, text, site), created_at=ago(12))
        rows.append(company)
    rows[4].preference_check = [{"preference_id": 1, "description": "Avoid the defense industry", "matched": True, "severity": "hard",
                                 "evidence_snippet": "Savunma sanayi için gömülü sistemler", "source_url": "https://anka-savunma.example/",
                                 "source_url_verified": True, "confidence": "high"}]
    db.add_all(rows)
    db.commit()
    goruntu, veridata, rota, medvizyon = rows[:4]

    letter = (
        "Merhaba,\n\nTekstil ve otomotiv üretim hatları için geliştirdiğiniz hata tespit sistemleri dikkatimi çekti. "
        "Görüntü işleme ve derin öğrenme üzerine çalışan yeni mezun bir bilgisayar mühendisiyim.\n\n"
        "Ekokardiyografi videolarında sol ventrikülü bölütleyen bir U-Net modeli geliştirdim ve CAMUS veri setinde 0.931 Dice "
        "skoruna ulaştım. Modeli bir FastAPI servisiyle sundum. Bu projede ekip liderliği yaparak üç kişilik bir ekibi yönettim.\n\n"
        "Junior Computer Vision Engineer pozisyonu için CV'mi ekte bulabilirsiniz. Görüşme fırsatı için teşekkür ederim.\n\n"
        "Saygılarımla,\nAda Demir"
    )
    claim_check = {"checked": 6, "supported": 5, "unsupported": [
        {"sentence": "Bu projede ekip liderliği yaparak üç kişilik bir ekibi yönettim.", "reason": "owner_claim_unverified"}]}
    db.add_all([
        Application(company_id=goruntu.id, cv_id=cv.id, state=ApplicationState.PENDING_APPROVAL, target_position="Junior Computer Vision Engineer",
                    subject="Junior Computer Vision Engineer başvurusu - Ada Demir", cover_letter_body=letter,
                    sources=["https://goruntu.example/"], application_reason="The careers page lists a junior computer vision role; the segmentation project is the closest match.",
                    evidence=[{"item_id": echo.id, "source_ref": "ada-demo/echo-ef", "source_type": "github_repo", "used": True,
                               "purpose": "Left-ventricle segmentation", "why": "segmentation experience maps to defect detection"},
                              {"item_id": fleet.id, "source_ref": "ada-demo/fleet-api", "source_type": "github_repo", "used": False,
                               "purpose": "Route planning API"}],
                    revision_history=[{"reasoning": "Segmentation of medical images is the closest thing to visual defect detection.",
                                       "claim_check": claim_check,
                                       "angles": [{"company_hook": "defect detection for textile and automotive production lines", "hook_url": "https://goruntu.example/",
                                                   "bridge": "segmentation -> defect detection", "owner_evidence_id": echo.id, "position": "Junior Computer Vision Engineer"},
                                                  {"company_hook": "deep learning", "hook_url": "https://goruntu.example/", "bridge": "model serving with FastAPI",
                                                   "owner_evidence_id": echo.id, "position": "ML Engineer"}]}],
                    last_action_at=ago(0.1)),
        Application(company_id=medvizyon.id, cv_id=cv.id, state=ApplicationState.AWAITING_REPLY, target_position="ML Engineer",
                    subject="ML Engineer başvurusu", cover_letter_body="Merhaba, ...", sent_at=ago(16), last_action_at=ago(16),
                    gmail_thread_id="demo-thread-1", sent_to="kariyer@medvizyon.example"),
        Application(company_id=veridata.id, cv_id=cv.id, state=ApplicationState.INTERVIEW, target_position="Junior Data Scientist",
                    subject="Junior Data Scientist", cover_letter_body="Merhaba, ...", sent_at=ago(30), replied_at=ago(24), last_action_at=ago(24),
                    gmail_thread_id="demo-thread-2", sent_to="careers@veridata.example"),
        Application(company_id=rota.id, cv_id=cv.id, state=ApplicationState.REPLIED, target_position="Backend Developer",
                    subject="Backend Developer", cover_letter_body="Merhaba, ...", sent_at=ago(21), replied_at=ago(18), last_action_at=ago(18),
                    gmail_thread_id="demo-thread-3", sent_to="hr@rota.example"),
    ])

    postings = [
        ("Junior Computer Vision Engineer", goruntu, "Ankara, Turkey", "onsite", "ats_api", True, {"verdict": "strong", "strengths": [
            {"point": "Segmentation project with a measured result", "posting_quote": "experience with image segmentation"}],
            "gaps": [{"point": "No production deployment on factory hardware", "posting_quote": "edge deployment is a plus"}]}),
        ("Machine Learning Engineer", None, "Remote - EMEA", "remote", "ats_api", True, {"verdict": "possible"}),
        ("Data Scientist", veridata, "Istanbul (Hybrid)", "hybrid", "careers_page", True, {}),
        ("Junior Backend Developer (Python)", rota, "Ankara", "hybrid", "ats_api", True, {}),
        ("ML Engineer", None, "Remote - Canada", "remote", "ats_api", True, {}),
        ("Senior Data Engineer", None, "Ankara", "onsite", "ats_api", True, {}),
        ("Computer Vision Intern", medvizyon, "Ankara", "onsite", "careers_page", False, {"verdict": "strong"}),
    ]
    for i, (title, company, location, mode, source, inbox, fit) in enumerate(postings):
        db.add(JobPosting(title=title, company_id=company.id if company else None, company_name="Northwind AI" if company is None else company.name,
                          url=f"https://jobs.example/{i}", location=location, work_mode=mode, source=source, inbox=inbox,
                          status=PostingStatus.NEW, fit=fit, gates={"role": {"result": "pass"}},
                          description_text=f"{title}\n\nWe are looking for an engineer to join our team in {location}. "
                                           "You will build and ship models with Python and PyTorch.",
                          requirements=["Python", "PyTorch or TensorFlow", "Experience with image segmentation"] if i == 0 else [],
                          extra={"parsed": i != 0} if i else {"parsed": True}))
    db.add_all([
        AtsBoard(provider="greenhouse", token="northwind", company_name="Northwind AI", found_via="search",
                 last_checked_at=ago(1), last_result={"read": 48, "imported": 3, "already_known": 5, "role": 30, "level": 7, "location": 3}),
        AtsBoard(provider="lever", token="goruntu", company_name="Görüntü Analitik", company_id=goruntu.id, found_via="research",
                 last_checked_at=ago(1), last_result={"read": 6, "imported": 1, "already_known": 2, "role": 3, "level": 0, "location": 0}),
    ])

    source = DiscoverySource(name="ODTÜ Teknokent", city="Ankara", url="https://teknokent.example/firmalar", kind="odtu", last_imported_at=ago(3))
    db.add(source)
    db.commit()
    for name, site, sector, text, status, triage in [
        ("Optik Zeka", "https://optikzeka.example", "Bilişim", "Yapay zeka ve görüntü işleme ile kalite kontrol", "ALIVE",
         {"fit": "high", "summary": "Visual quality control with deep learning for manufacturing.", "fit_reason": "Computer vision roles match your targets."}),
        ("Sayısal Tarım", "https://sayisaltarim.example", "", "Tarım için uydu görüntüsü analitiği ve veri platformu", "ALIVE", None),
        ("Yapı Proje İnşaat", "https://yapiproje.example", "İnşaat", "İnşaat taahhüt ve proje yönetimi", "ALIVE", None),
        ("Eski Yazılım Ltd.", "https://eskiyazilim.example", "Yazılım", "", "DEAD", None),
    ]:
        db.add(CompanyCandidate(source_id=source.id, name=name, website_url=site, sector=sector, technopark="ODTÜ Teknokent", city="Ankara",
                                status=CandidateStatus.PENDING, liveness={"status": status, "text": text, "signals": []},
                                triage=triage or {}, triaged_at=ago(1) if triage else None))

    for days, purpose, model, tin, tout in [(1, "draft_letter", "claude-sonnet-5-5", 9000, 900), (1, "research", "claude-haiku-4-5", 12000, 600),
                                            (5, "repo_record", "claude-sonnet-5-5", 30000, 2500), (8, "triage", "claude-haiku-4-5", 4000, 300)]:
        db.add(LLMCallLog(purpose=purpose, provider="claude", model=model, input_tokens=tin, output_tokens=tout, created_at=ago(days)))
    db.commit()
    db.close()


PAGES = [
    ("today", "/", None),
    ("postings", "/postings?seg=new&open=1", None),
    ("posting-filter", "/postings?seg=new", "document.getElementById('posting-filter').open = true"),
    ("application", "/applications/1", None),
    ("companies", "/companies?open=1", None),
    ("discovery", "/discovery?sector=all", "window.scrollTo(0, document.querySelector('ol.flow > li:nth-child(2)').offsetTop - 70)"),
    ("profile", "/profile", None),
    ("cv-builder", "/cv-builder", None),
    ("settings", "/preferences", None),
]


async def capture(pages: list[tuple[str, str, str | None]]) -> None:
    import websockets

    profile = WORK / "chrome"
    proc = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--remote-debugging-port=9444",
                             f"--user-data-dir={profile}", "--window-size=1440,960", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen("http://127.0.0.1:9444/json"))
                break
            except OSError:
                time.sleep(0.25)
        ws_url = next(t for t in tabs if t["type"] == "page")["webSocketDebuggerUrl"]
        async with websockets.connect(ws_url, max_size=64_000_000) as ws:
            counter = 0

            async def call(method: str, **params):
                nonlocal counter
                counter += 1
                await ws.send(json.dumps({"id": counter, "method": method, "params": params}))
                while True:
                    message = json.loads(await ws.recv())
                    if message.get("id") == counter:
                        return message

            await call("Page.enable")
            await call("Emulation.setDeviceMetricsOverride", width=1440, height=960, deviceScaleFactor=1, mobile=False)
            for name, path, script in pages:
                await call("Page.navigate", url=f"http://127.0.0.1:{PORT}{path}")
                await asyncio.sleep(2.0)  # the CV preview renders its PDF
                if script:
                    await call("Runtime.evaluate", expression=script)
                    await asyncio.sleep(0.6)
                shot = await call("Page.captureScreenshot", format="png")
                (OUT / f"{name}.png").write_bytes(base64.b64decode(shot["result"]["data"]))
                print("captured", name)
    finally:
        proc.kill()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    seed()
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)], cwd=ROOT,
                              env={**os.environ, **ENV}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/")
                break
            except OSError:
                time.sleep(0.25)
        asyncio.run(capture(PAGES))
    finally:
        server.terminate()


if __name__ == "__main__":
    main()
