"""Roadmap phase 5: bullet bank, tech-stack list, code assembly, Typst render
in the owner's layout, ATS text-layer check, approval and attachment."""
import json

import pytest
from fastapi.testclient import TestClient

import app.applications.review as review
from app.cv_builder import assemble as assemble_mod
from app.cv_builder.ats import check
from app.cv_builder.bank import edit_bullet, generate_bullets, suggest_projects
from app.cv_builder.render import render
from app.cv_builder.service import approve, create_for_application, render_generated
from app.cv_builder.skills import evidence_status, rebuild_skills, selected_project_item_ids
from app.db import SessionLocal, init_db
from app.main import app
from app.models import (
    CV, Application, ApplicationState, Bullet, Company, CVProject, GeneratedCV, JobPosting, OwnerSettings,
    ProfileItem, ProfileItemStatus, TechSkill,
)
from tests.conftest import make_cv_file
from tests.fakes import FakeLLMProvider

CV_PATH = "/tmp/owner-base-cv-test.pdf"


@pytest.fixture(scope="module")
def bank():
    init_db()
    db = SessionLocal()
    for model in (Bullet, CVProject, TechSkill, GeneratedCV):
        db.query(model).delete()
    db.commit()
    cv = CV(name="Base CV test", target_role="ML", file_path=make_cv_file("base-cv-test.pdf"))
    db.add(cv)
    db.commit()
    entries = [
        ("education", {"title": "B.Sc. Computer Engineering", "organization": "Ankara University", "start": "09/2022", "end": "06/2026", "location": "Ankara, Turkey", "bullets": ["GPA: 3.58 / 4.00"]}),
        ("experience", {"title": "Data Science Intern", "organization": "AppNava", "start": "07/2025", "end": "09/2025", "location": "",
                        "bullets": ["Processed 1.5M+ user records from BigQuery on Google Cloud.", "Developed a two-stage LTV prediction pipeline with LightGBM, improving mid-LTV segment accuracy by 4%."]}),
        ("certificate", {"title": "SQL (Advanced) Certificate", "organization": "HackerRank", "start": "", "end": "", "location": "", "bullets": []}),
    ]
    for kind, entry in entries:
        db.add(ProfileItem(source_type="cv", source_ref=f"{cv.file_path}#{kind}", parent_ref=cv.file_path, status=ProfileItemStatus.ANALYZED,
                           extracted={"entry_type": kind, **entry, "purpose": "x", "stack": [], "role": "x", "highlights": []}))
    echo = ProfileItem(source_type="github_repo", source_ref="owner/echo-ef", status=ProfileItemStatus.ANALYZED, source_urls=["https://github.com/owner/echo-ef"],
                       extracted={"one_liner": "Echocardiography LV segmentation", "domains": ["medical imaging"],
                                  "results": [{"claim": "Dice 0.931 on CAMUS", "quote": "Dice 0.931", "source": "readme"}],
                                  "maturity": {"tests": True}, "role_angles": {"computer_vision": "U-Net segmentation"}},
                       facts={"stack": ["Python", "PyTorch", "FastAPI"], "activity": {"stars": 3}})
    rag = ProfileItem(source_type="github_repo", source_ref="owner/rag-qa", status=ProfileItemStatus.ANALYZED,
                      extracted={"one_liner": "Multi document RAG", "domains": ["generative ai"]}, facts={"stack": ["Python", "LangChain", "FAISS"]})
    settings = db.query(OwnerSettings).first() or OwnerSettings()
    settings.full_name, settings.title, settings.email, settings.phone = "Hasan Kalin", "Computer Engineer", "hsnkln4@gmail.com", "+905386270481"
    settings.base_cv_id = cv.id
    settings.cv_template_approved = False
    db.add_all([echo, rag, settings])
    db.commit()
    yield db, cv, echo, rag
    settings.full_name = settings.title = settings.email = settings.phone = ""
    settings.base_cv_id = None
    db.query(ProfileItem).filter((ProfileItem.parent_ref == cv.file_path) | ProfileItem.source_ref.in_(["owner/echo-ef", "owner/rag-qa"])).delete(synchronize_session=False)
    for model in (Bullet, CVProject, TechSkill, GeneratedCV):
        db.query(model).delete()
    db.commit()
    db.close()


BULLETS = json.dumps({"bullets": [
    {"text": "Built a U-Net pipeline estimating ejection fraction from echocardiography video, reaching Dice 0.931 on CAMUS.",
     "tech": ["PyTorch"], "domain": ["medical imaging"], "role_types": ["computer_vision"], "source_quote": "Dice 0.931", "source_kind": "result", "metric": "real"},
    {"text": "Served the models through a FastAPI backend used by 500 clinicians.",
     "tech": ["FastAPI"], "domain": ["medical imaging"], "role_types": ["backend_data"], "source_quote": "x", "source_kind": "record", "metric": "real"},
    {"text": "Deployed an explainable full-stack app with an annotated overlay video.",
     "tech": [], "domain": [], "role_types": ["computer_vision"], "source_quote": "x", "source_kind": "record", "metric": "none"},
]})


def test_projects_bullets_and_owner_locks(bank):
    db, cv, echo, rag = bank
    projects = suggest_projects(db)
    by_item = {p.profile_item_id: p for p in projects}
    assert by_item[echo.id].score > by_item[rag.id].score
    rag_project = by_item[rag.id]
    rag_project.selected, rag_project.owner_decided = False, True
    db.commit()
    suggest_projects(db)
    assert db.get(CVProject, rag_project.id).selected is False  # the owner's choice wins

    project = by_item[echo.id]
    bullets = generate_bullets(db, FakeLLMProvider(BULLETS), project)
    flags = {b.text[:10]: b.flags for b in bullets}
    assert flags["Built a U-"]["ungrounded_numbers"] == []
    assert flags["Served the"]["ungrounded_numbers"] == ["500"]  # never invented silently
    assert flags["Deployed a"]["needs_metric"] is True
    assert all(b.status == "draft" for b in bullets)

    first, second = bullets[0], bullets[1]
    edit_bullet(db, second, "Served the models through a FastAPI backend with per-cycle EF output.")
    first.status = "approved"
    db.commit()
    again = generate_bullets(db, FakeLLMProvider(BULLETS), project)
    locked = next(b for b in again if b.id == second.id)
    assert locked.locked and locked.text.startswith("Served the models through a FastAPI backend with")
    assert locked.pending_text.startswith("Served the models through a FastAPI backend used by 500")
    assert any(b.id == first.id and b.status == "approved" for b in again)


def test_tech_list_evidence_and_removed_projects(bank):
    db, cv, echo, rag = bank
    skills = {s.name: s for s in rebuild_skills(db)}
    assert {"PyTorch", "FastAPI", "LangChain", "BigQuery", "LightGBM"} <= set(skills)
    assert skills["BigQuery"].evidence[0]["kind"] == "experience"  # only on the CV
    assert skills["Python"].group == "languages" and skills["LangChain"].group == "genai"
    selected = selected_project_item_ids(db)
    assert evidence_status(skills["LangChain"], selected) == "evidence_removed"  # RAG project deselected
    assert evidence_status(skills["PyTorch"], selected) == "ok"


def test_assembly_render_and_ats_check(bank):
    db, cv, echo, rag = bank
    for bullet in db.query(Bullet).all():
        bullet.status = "approved"
    db.commit()
    posting = JobPosting(title="Computer Vision Engineer", requirements=["PyTorch", "segmentation experience", "Kubernetes"],
                         nice_to_have=[], description_text="PyTorch, segmentation, Kubernetes. Certification is a plus.")
    selection = assemble_mod.assemble(db, posting)
    assert selection["skills"]["frameworks"][0] == "PyTorch"  # matched first
    assert "LangChain" not in selection["skills"]["genai"]  # evidence removed with the project
    assert selection["certificates"] is True and len(selection["project_ids"]) == 1

    generated = GeneratedCV(selection=selection, language="en")
    db.add(generated)
    db.commit()
    pdf, data = render(db, generated)
    report = check(pdf, data, selection["keywords"])
    assert report["page_count"] == 1
    assert report["passed"], report["checks"]
    assert "Kubernetes" in report["keyword_report"]["missing"]  # a gap, never added
    assert "mid-ltv" not in report["word_diffs"]
    sections = [s["kind"] for s in data["sections"]]
    assert sections == ["education", "experience", "skills", "projects", "certificates"]
    assert data["sections"][1]["entries"][0]["bullets"][1].startswith("Developed a two-stage LTV")  # verbatim


def test_approval_needs_ats_and_template_and_the_pdf_is_attached(bank, monkeypatch):
    db, cv, echo, rag = bank
    company = Company(name="CVB Co", contact_email="jobs@cvb.example")
    db.add(company)
    db.commit()
    application = Application(company_id=company.id, cv_id=cv.id, subject="S", cover_letter_body="B", state=ApplicationState.PENDING_APPROVAL)
    db.add(application)
    db.commit()
    generated = create_for_application(db, application)
    with pytest.raises(ValueError):
        approve(db, generated)  # not rendered yet
    render_generated(db, generated)
    client = TestClient(app)
    response = client.post(f"/cv-builder/cvs/{generated.id}/approve")
    assert "template" in str(response.url)  # template not approved yet
    client.post("/cv-builder/template/approve")
    client.post(f"/cv-builder/cvs/{generated.id}/approve")
    db.refresh(application)
    db.refresh(generated)
    assert generated.status == "approved" and application.generated_cv_id == generated.id

    sent = {}
    monkeypatch.setattr(review, "send_email", lambda **kw: sent.update(kw) or {"id": "m", "threadId": "t"})
    review.approve_and_send(db, application, "jobs@cvb.example")
    assert sent["attachment_name"] == "Hasan_Kalin_CV.pdf"
    with open(sent["attachment_path"], "rb") as f:
        assert f.read() == open(generated.pdf_path, "rb").read()
    assert application.cv_snapshot["generated_cv_id"] == generated.id


def test_cv_builder_pages_render(bank):
    db, cv, echo, rag = bank
    client = TestClient(app)
    assert client.get("/cv-builder").status_code == 200
    generated = db.query(GeneratedCV).filter(GeneratedCV.pdf_path != "").first()
    page = client.get(f"/cv-builder/cvs/{generated.id}")
    assert page.status_code == 200 and "PyTorch" in page.text
    assert client.get(f"/cv-builder/cvs/{generated.id}/pdf").headers["content-type"] == "application/pdf"


def test_consistency_check_flags_different_numbers_for_the_same_metric():
    from app.cv_builder.consistency import metrics_in

    assert metrics_in("Achieved Dice 0.931, EF MAE 5.43 and ROC-AUC 96.26%") == {"dice": {0.93}, "mae": {5.43}, "roc-auc": {0.96}}
    db = SessionLocal()
    try:
        item = ProfileItem(source_type="github_repo", source_ref="owner/consist", status=ProfileItemStatus.ANALYZED,
                           extracted={"one_liner": "x", "results": [{"claim": "Dice 0.931", "quote": "Dice 0.931", "source": "readme"}]})
        db.add(item)
        db.commit()
        project = CVProject(profile_item_id=item.id, name="consist", selected=True)
        db.add(project)
        db.commit()
        db.add(Bullet(project_id=project.id, text="Reached Dice 0.89 on CAMUS", status="approved"))
        db.commit()
        from app.cv_builder.consistency import check_consistency

        issues = [i for i in check_consistency(db) if i["project"] == "consist"]
        assert issues and issues[0]["metric"] == "dice" and issues[0]["values"] == {"record": [0.93], "bullets": [0.89]}
    finally:
        db.query(Bullet).filter(Bullet.text == "Reached Dice 0.89 on CAMUS").delete()
        db.query(CVProject).filter(CVProject.name == "consist").delete()
        db.query(ProfileItem).filter(ProfileItem.source_ref == "owner/consist").delete()
        db.commit()
        db.close()
