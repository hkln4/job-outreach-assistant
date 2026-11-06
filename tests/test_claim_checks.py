"""Hallucination guards: every letter sentence is checked against its source
by code; model-written text (research summary, role angles, talking points)
never reaches a later prompt as a fact unless code verified it."""
import json

from fastapi.testclient import TestClient

from app.applications import drafting
from app.applications.claims import check_claims, sentences
from app.applications.draft_checks import draft_warnings
from app.applications.evidence import _rich_block, citable_text
from app.db import SessionLocal, init_db
from app.main import app
from app.models import CV, Company, Profile, ProfileItem, ProfileItemStatus
from app.web.deps import get_draft_llm
from tests.conftest import make_cv_file
from tests.fakes import RoutedLLMProvider

PAGES = [
    {"url": "https://cc.example/", "kind": "home", "text": "We build defect detection for textile factories across Turkey. " * 4},
    {"url": "https://cc.example/careers", "kind": "careers", "text": "Our computer vision team is hiring new graduates. " * 4},
]


def _repo(item_id: int = 90_001) -> ProfileItem:
    return ProfileItem(
        id=item_id, source_type="github_repo", source_ref="owner/seg", status=ProfileItemStatus.ANALYZED,
        extracted={"one_liner": "Lesion segmentation with a U-Net",
                   "results": [{"claim": "Dice 0.87", "quote": "Dice score of 0.87", "source": "readme"}],
                   "role_angles": {"computer_vision": "Deployed to production for 500 users."},
                   "talking_points": ["Chose Dice loss for class imbalance.", "Led a team of five engineers."]},
        facts={"stack": ["Python", "PyTorch"]},
    )


def test_each_sentence_needs_a_quote_in_its_own_source():
    repo = _repo()
    body = ("Merhaba,\nTekstil fabrikaları için hata tespiti yapan sisteminiz dikkatimi çekti. "
            "Bir U-Net ile lezyon segmentasyonu yaptım ve Dice 0.87 elde ettim. "
            "Bu sistem üretimde 500 kullanıcıya hizmet verdi. "
            "Ekte CV'mi bulabilirsiniz.")
    claims = [
        {"sentence": "Tekstil fabrikaları için hata tespiti yapan sisteminiz dikkatimi çekti.", "kind": "company",
         "supports": [{"source": "https://cc.example/careers", "quote": "defect detection for textile factories"}]},  # wrong page -> corrected
        {"sentence": "Bir U-Net ile lezyon segmentasyonu yaptım ve Dice 0.87 elde ettim.", "kind": "owner",
         "supports": [{"source": f"E{repo.id}", "quote": "Dice score of 0.87"}]},
        {"sentence": "Bu sistem üretimde 500 kullanıcıya hizmet verdi.", "kind": "owner",
         "supports": [{"source": f"E{repo.id}", "quote": "Deployed to production for 500 users"}]},  # model framing is not citable
        {"sentence": "Ekte CV'mi bulabilirsiniz.", "kind": "none", "supports": []},
    ]
    result = check_claims(body, claims, PAGES, [repo], "CC Co")
    assert result["checked"] == 4
    assert result["unsupported"] == [{"sentence": "Bu sistem üretimde 500 kullanıcıya hizmet verdi.", "reason": "owner_claim_unverified"}]
    company_support = result["sentences"][0]["supports"][0]
    assert company_support["source"] == "https://cc.example/"


def test_the_model_cannot_wave_a_factual_sentence_through():
    body = "Şirketiniz 2019'dan beri 40 ülkeye ihracat yapıyor. Görüşmek isterim, teşekkürler."
    claims = [{"sentence": "Şirketiniz 2019'dan beri 40 ülkeye ihracat yapıyor.", "kind": "none", "supports": []}]
    result = check_claims(body, claims, PAGES, [], "CC Co")
    assert result["unsupported"] == [{"sentence": "Şirketiniz 2019'dan beri 40 ülkeye ihracat yapıyor.", "reason": "factual_without_source"}]
    # a sentence left out of the annotation is unsupported too, when it looks factual
    result = check_claims("I used PyTorch and Kubernetes at scale for years.", [], PAGES, [], "CC Co")
    assert result["unsupported"][0]["reason"] == "not_annotated"
    assert sentences("Hi.\nThanks a lot for reading. Bye now.") == ["Thanks a lot for reading."]


def test_model_framing_is_filtered_and_labelled():
    repo = _repo()
    block = _rich_block(repo, "computer_vision")
    assert "Deployed to production for 500 users" not in block  # unbacked number + risky claim
    assert "Led a team of five engineers" not in block  # risky claim, no fact backs it
    assert "TALKING POINT: Chose Dice loss for class imbalance." in block
    assert block.index("FACTS (citable):") < block.index("MODEL FRAMING")
    assert "Deployed to production" not in citable_text(repo)
    repo.owner_edited_fields = ["talking_points"]
    assert "Led a team of five engineers" in _rich_block(repo, "computer_vision")  # the owner's own words


def test_draft_stores_the_sentence_check_and_edits_mark_it_stale():
    init_db()
    db = SessionLocal()
    try:
        if db.query(Profile).first() is None:
            db.add(Profile(summary="Engineer.", skills=[], target_roles=["CV Engineer"]))
        db.add(CV(name="CC CV", file_path=make_cv_file("cc.pdf")))
        company = Company(name="CC Co", website_url="https://cc.example", research={"pages": PAGES, "content_chars": 800})
        db.add(company)
        db.commit()
        letter = "Subject: Junior CV Engineer Başvurusu\n\nMerhaba,\nTekstil fabrikaları için hata tespiti yapan sisteminiz dikkatimi çekti. Şirketiniz 40 ülkede faaliyet gösteriyor."
        meta = {"sources": [], "cv_choice_name": "CC CV", "claims": [
            {"sentence": "Tekstil fabrikaları için hata tespiti yapan sisteminiz dikkatimi çekti.", "kind": "company",
             "supports": [{"source": "https://cc.example/", "quote": "defect detection for textile factories"}]},
            {"sentence": "Şirketiniz 40 ülkede faaliyet gösteriyor.", "kind": "company", "supports": [{"source": "https://cc.example/", "quote": "operates in 40 countries"}]},
        ]}
        llm = RoutedLLMProvider({"planning a job application email": json.dumps({"angles": []}), "Output plain text only": letter,
                                 "annotating a finished job application email": json.dumps(meta),
                                 "choosing which of a job applicant": json.dumps({"item_ids": []})})
        application = drafting.create_draft_application(db, llm, company)
        check = application.revision_history[0]["claim_check"]
        assert check["supported"] == 1 and check["unsupported"][0]["sentence"] == "Şirketiniz 40 ülkede faaliyet gösteriyor."
        assert application.sources == ["https://cc.example/"]  # verified page support becomes a source
        keys = [key for key, _ in draft_warnings(application)]
        assert "draft_check.claim.company_claim_unverified" in keys

        app.dependency_overrides[get_draft_llm] = lambda: llm
        client = TestClient(app)
        client.post(f"/applications/{application.id}/edit", data={"subject": application.subject, "cover_letter_body": application.cover_letter_body + " Ek.", "target_position": ""})
        db.refresh(application)
        assert application.revision_history[0]["claim_check"]["stale"] is True
        client.post(f"/applications/{application.id}/recheck-claims")
        db.refresh(application)
        assert "stale" not in application.revision_history[0]["claim_check"]
    finally:
        app.dependency_overrides.pop(get_draft_llm, None)
        db.close()
