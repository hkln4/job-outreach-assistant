"""Roadmap phase 4: angles verified by code, plain-text prose + metadata call,
app-written signature and portfolio link, style memory, recipient tone,
grounding audit, reviewer edits applied one by one, fact capture."""
import json

import pytest
from fastapi.testclient import TestClient

from app.applications import drafting
from app.applications.angles import verify_angles
from app.applications.draft_review import apply_issue
from app.applications.facts import approve_fact, propose_facts, worth_checking
from app.applications.grounding import grounding_misses
from app.applications.letter import parse_letter
from app.applications.phrases import found_in
from app.applications.signature import signature_block, with_signature
from app.db import SessionLocal, init_db
from app.main import app
from app.models import (
    CV, Application, ApplicationState, Company, Contact, OwnerSettings, Profile, ProfileFact,
    ProfileFactStatus, ProfileItem, ProfileItemStatus, StyleExample, StyleNote,
)
from app.web.deps import get_draft_llm, get_llm
from tests.conftest import make_cv_file
from tests.fakes import FakeLLMProvider, RoutedLLMProvider

PAD = " The team ships production software for logistics customers and maintains its own platform in Ankara." * 3
PAGES = [
    {"url": "https://lq.example/", "kind": "home", "text": "We build defect detection for textile factories." + PAD},
    {"url": "https://lq.example/kariyer", "kind": "careers", "text": "Bilgisayarlı görü ekibimize yeni mezun arıyoruz." + PAD},
]


@pytest.fixture
def seeded():
    init_db()
    db = SessionLocal()
    try:
        if db.query(Profile).first() is None:
            db.add(Profile(summary="Computer engineer.", skills=[{"skill": "Python", "evidence_source": "x"}], target_roles=["CV Engineer"]))
        db.add(CV(name="LQ CV", target_role="CV Engineer", file_path=make_cv_file("lq.pdf")))
        company = Company(name="LQ Co", website_url="https://lq.example", contact_email="ik@lq.example", research={"pages": PAGES, "content_chars": 1200})
        repo = ProfileItem(source_type="github_repo", source_ref="owner/lesion-seg", status=ProfileItemStatus.ANALYZED,
                           extracted={"one_liner": "Lesion segmentation", "results": [{"claim": "Dice 0.87", "quote": "Dice 0.87", "source": "readme"}]},
                           facts={"stack": ["Python", "PyTorch"]})
        db.add_all([company, repo])
        settings = db.query(OwnerSettings).first() or OwnerSettings()
        settings.full_name, settings.phone, settings.portfolio_url = "Hasan Kalın", "+905000000000", "https://hasan.example"
        db.add(settings)
        db.commit()
        page = ProfileItem(source_type="site", source_ref="https://hasan.example/projects/lesion-seg", status=ProfileItemStatus.ANALYZED,
                           extracted={"purpose": "Lesion segmentation project page", "stack": [], "role": "author", "highlights": []})
        db.add(page)
        db.commit()
        yield db, company, repo
    finally:
        settings = db.query(OwnerSettings).first()
        settings.full_name = settings.phone = settings.portfolio_url = ""
        db.query(ProfileItem).filter(ProfileItem.source_ref.in_(["owner/lesion-seg", "https://hasan.example/projects/lesion-seg"])).delete(synchronize_session=False)
        db.query(StyleNote).delete()
        db.query(StyleExample).delete()
        db.commit()
        db.close()


def _routes(repo_id: int, letter: str = "Subject: Junior CV Engineer Başvurusu\n\nMerhaba,\nTekstil hatalarını tespit eden sisteminiz için yazıyorum. Dice 0.87 ve 95% doğruluk aldım, Kubernetes kullandım.\n\nHasan Kalın") -> RoutedLLMProvider:
    angles = {"angles": [
        {"company_hook": "defect detection for textile factories", "hook_url": "https://lq.example/kariyer", "owner_evidence_id": repo_id, "bridge": "b", "position": "CV Engineer"},
        {"company_hook": "made up hook", "hook_url": "https://lq.example/", "owner_evidence_id": repo_id, "bridge": "b", "position": "x"},
        {"company_hook": "Bilgisayarlı görü ekibimize", "hook_url": "https://lq.example/kariyer", "owner_evidence_id": 999999, "bridge": "b", "position": "x"},
        {"company_hook": "Bilgisayarlı görü ekibimize yeni mezun", "hook_url": "https://lq.example/", "owner_evidence_id": repo_id, "bridge": "b2", "position": "CV Engineer"},
    ]}
    meta = {"target_position": "CV Engineer", "cv_choice_name": "LQ CV", "reasoning": "r", "sources": ["https://lq.example/", "https://invented.example"],
            "evidence_item_ids": [repo_id, 424242], "unsupported_claims": []}
    return RoutedLLMProvider({"planning a job application email": json.dumps(angles), "Output plain text only": letter,
                              "annotating a finished job application email": json.dumps(meta)})


def test_draft_is_angle_letter_metadata_with_app_written_signature(seeded):
    db, company, repo = seeded
    llm = _routes(repo.id)
    result = drafting.draft_application(db, llm, company, evidence_items=[repo])

    # angles: invented hook dropped, invalid id dropped, wrong hook_url corrected to the real page
    assert [a["company_hook"] for a in result["angles"]] == ["defect detection for textile factories", "Bilgisayarlı görü ekibimize yeni mezun"]
    assert result["angles"][0]["hook_url"] == "https://lq.example/" and result["angles"][1]["hook_url"] == "https://lq.example/kariyer"
    assert result["dropped_angles"] == 2
    # the letter call is plain text; the chosen angle reaches it
    (letter_call,) = llm.calls_for("Output plain text only")
    assert letter_call["json_schema"] is None and "CHOSEN ANGLE" in letter_call["prompt"]
    assert "do not write a signature" in letter_call["system"].casefold()
    assert "RECIPIENT:" in letter_call["prompt"]  # ik@ -> HR tone
    # metadata checked by code
    assert result["sources"] == ["https://lq.example/"] and result["dropped_sources"] == ["https://invented.example"]
    assert result["evidence_item_ids"] == [repo.id]
    # app-written parts: project page from the portfolio, signature; the model's own name line replaced
    body = result["cover_letter_body"]
    assert "Proje sayfası: https://hasan.example/projects/lesion-seg" in body
    assert body.rstrip().endswith("https://hasan.example") and body.count("Hasan Kalın") == 1
    # grounding audit: 95 and Kubernetes aren't in any source, 0.87 is
    assert result["grounding_misses"] == {"numbers": ["95"], "technologies": ["Kubernetes"]}


def test_regenerate_with_another_angle_reuses_saved_angles(seeded):
    db, company, repo = seeded
    application = drafting.create_draft_application(db, _routes(repo.id), company)
    assert application.revision_history[0]["angle_index"] == 0
    second = _routes(repo.id)
    drafting.regenerate_draft(db, second, application, "", angle_index=1)
    assert second.calls_for("planning a job application email") == []  # no new angle call
    assert "Bilgisayarlı görü ekibimize yeni mezun" in second.calls_for("Output plain text only")[0]["prompt"]
    assert application.revision_history[0]["angle_index"] == 1


def test_style_rules_and_examples_reach_the_writer(seeded):
    db, company, repo = seeded
    db.add(StyleNote(text="Never mention GPA"))
    db.add(StyleExample(language="tr", subject="Örnek konu", body="Merhaba, örnek mektup gövdesi."))
    db.commit()
    llm = _routes(repo.id)
    drafting.draft_application(db, llm, company, evidence_items=[repo])
    prompt = llm.calls_for("Output plain text only")[0]["prompt"]
    assert "- Never mention GPA" in prompt and "örnek mektup gövdesi" in prompt


def test_parse_letter_shapes():
    assert parse_letter("Subject: Hi there\n\nBody text") == {"subject": "Hi there", "body": "Body text", "metadata": None}
    assert parse_letter("Konu: Başvuru\nGövde")["subject"] == "Başvuru"
    legacy = parse_letter(json.dumps({"subject": "S", "cover_letter_body": "B", "sources": ["u"]}))
    assert legacy["metadata"]["sources"] == ["u"]
    with pytest.raises(ValueError, match="subject"):
        parse_letter("Just a body without a subject")


def test_verify_angles_and_signature_helpers():
    kept, dropped = verify_angles([{"company_hook": "textile factories", "hook_url": "x", "owner_evidence_id": 1}], PAGES, {1})
    assert kept[0]["hook_url"] == "https://lq.example/" and dropped == 0
    signature = signature_block({"full_name": "A B", "phone": "1", "email": "a@b.c", "portfolio_url": "https://p.example"}, "en")
    assert signature == "A B\nPhone: 1\na@b.c\nhttps://p.example"
    assert with_signature("Hello\n\nA B", signature).count("A B") == 1


def test_grounding_and_banned_phrases_are_code():
    misses = grounding_misses("I reached 92% with PyTorch and Rust in 2024.", "pytorch project, accuracy 92%, 2024")
    assert misses == {"numbers": [], "technologies": ["Rust"]}
    assert found_in("Değerli ekibinize katılmak için heyecan duyuyorum. I believe I can contribute.") == [
        "heyecan duyuyorum", "değerli ekibinize", "i believe i can contribute"]


def test_reviewer_edit_is_applied_individually():
    subject, body = "Başvuru", "Merhaba,\nDeğerli ekibinize   katılmak isterim.\nTeşekkürler"
    assert apply_issue(subject, body, {"quote": "Değerli ekibinize katılmak isterim.", "replacement": "Ekibinizle çalışmak isterim."}) == (
        "Başvuru", "Merhaba,\nEkibinizle çalışmak isterim.\nTeşekkürler")
    assert apply_issue(subject, body, {"quote": "not there", "replacement": "x"}) is None


def test_recipient_kind_uses_contact_roles(seeded):
    from app.applications.style import recipient_kind

    db, company, repo = seeded
    application = Application(company_id=company.id, state=ApplicationState.PENDING_APPROVAL)
    db.add(application)
    db.commit()
    db.add(Contact(company_id=company.id, name="Ayşe", role="CTO", email="ayse@lq.example", application_id=application.id))
    db.commit()
    assert recipient_kind(db, application, company) == "technical"
    assert recipient_kind(db, None, company) == "hr"


def test_fact_capture_proposes_and_approval_makes_an_evidence_record(seeded):
    db, company, repo = seeded
    assert not worth_checking("Make it shorter and warmer.")
    llm = FakeLLMProvider(json.dumps({"facts": [
        {"text": "The owner did a 6-month internship at Acme.", "quote": "Acme'de 6 ay staj yaptım"},
        {"text": "Invented", "quote": "words he never wrote"},
    ]}))
    (fact,) = propose_facts(db, llm, "Daha kısa yaz. Ayrıca Acme'de 6 ay staj yaptım, onu da ekle.", source_kind="feedback", application_id=None, known="")
    assert fact.status == ProfileFactStatus.PROPOSED
    item = approve_fact(db, fact)
    assert item.source_type == "owner" and item.status == ProfileItemStatus.ANALYZED
    assert item.owner_facts[0]["answer"] == "The owner did a 6-month internship at Acme."
    db.delete(item)
    db.delete(fact)
    db.commit()


def test_review_page_and_style_page_render_with_angles(seeded):
    db, company, repo = seeded
    application = drafting.create_draft_application(db, _routes(repo.id), company)
    client = TestClient(app)
    page = client.get(f"/applications/{application.id}")
    assert page.status_code == 200 and "defect detection for textile factories" in page.text
    assert client.post(f"/applications/{application.id}/good-example").status_code == 200
    assert client.get("/style").status_code == 200
    assert db.query(StyleExample).filter(StyleExample.application_id == application.id).count() == 1

    app.dependency_overrides[get_llm] = lambda: FakeLLMProvider(json.dumps({"facts": []}))
    app.dependency_overrides[get_draft_llm] = lambda: _routes(repo.id)
    try:
        response = client.post(f"/applications/{application.id}/edit", data={"subject": application.subject, "cover_letter_body": "Edited body with 12 projects.", "target_position": ""})
        assert response.status_code == 200
        db.refresh(application)
        assert any(entry.get("edit_diff") for entry in application.revision_history[1:])
        assert application.revision_history[0]["grounding_misses"]["numbers"] == ["12"]
        assert client.post(f"/applications/{application.id}/angle", data={"angle_index": 1}).status_code == 200
    finally:
        app.dependency_overrides.pop(get_llm, None)
        app.dependency_overrides.pop(get_draft_llm, None)
    assert db.query(ProfileFact).count() >= 0
