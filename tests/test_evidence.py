"""Profile evidence retrieval: the app owns the candidate set, the LLM only
ranks it, and anything outside the set is dropped - same for the ids the draft
claims to have used. Regenerate reuses the saved pick unless the owner
excluded one of its records."""
import json

import pytest
from fastapi.testclient import TestClient

from app.applications import evidence as evidence_mod
from app.applications.drafting import create_draft_application, regenerate_draft
from app.db import SessionLocal, init_db
from app.main import app
from app.models import CV, Application, Company, Profile, ProfileItem, ProfileItemStatus
from tests.fakes import SequencedLLMProvider

client = TestClient(app)

PAGE = (
    "We build computer vision systems for factories in Ankara: defect detection on production lines, "
    "written in Python with PyTorch, deployed on edge devices. We are hiring a junior ML engineer. "
    "The team publishes case studies about the inspection systems it has delivered to customers."
)


def setup_module():
    init_db()


@pytest.fixture
def seeded():
    """More analyzed records than MAX_EVIDENCE, so a real selection happens.
    Removed afterwards: other test modules read llm.calls[0] as the draft call."""
    db = SessionLocal()
    try:
        if db.query(Profile).first() is None:
            db.add(Profile(summary="ML engineer.", skills=[], target_roles=["ML Engineer"]))
        if db.query(CV).filter(CV.name == "Evidence CV").first() is None:
            db.add(CV(name="Evidence CV", target_role="ML Engineer", file_path="data/cv_variants/evidence.pdf"))
        company = Company(
            name="Vision Co",
            website_url="https://vision.example",
            contact_email="jobs@vision.example",
            research={"pages": [{"url": "https://vision.example", "text": PAGE}], "summary": "Factory CV systems."},
        )
        db.add(company)
        items = [
            ProfileItem(
                source_type="cv",
                source_ref="data/cv.pdf",
                extracted={"purpose": "CV", "stack": ["Python"], "role": "student", "highlights": []},
                status=ProfileItemStatus.ANALYZED,
            ),
            ProfileItem(
                source_type="linkedin",
                source_ref="data/linkedin.pdf",
                extracted={"purpose": "LinkedIn", "stack": [], "role": "student", "highlights": []},
                status=ProfileItemStatus.ANALYZED,
            ),
        ]
        for n in range(evidence_mod.MAX_EVIDENCE + 2):
            items.append(
                ProfileItem(
                    source_type="github_repo",
                    source_ref=f"owner/repo-{n}",
                    extracted={"purpose": f"Repo {n}", "stack": ["Python"], "role": "author", "highlights": []},
                    status=ProfileItemStatus.ANALYZED,
                    source_urls=[f"https://github.com/owner/repo-{n}"],
                )
            )
        db.add_all(items)
        db.commit()
        ids = {item.source_ref: item.id for item in items}
        yield db, company, ids
    finally:
        db.query(ProfileItem).filter(ProfileItem.id.in_(list(ids.values()))).delete()
        db.commit()
        db.close()


def _draft_json(used_ids):
    return json.dumps(
        {
            "target_position": "ML Engineer",
            "cv_choice_name": "Evidence CV",
            "reasoning": "Fits.",
            "subject": "Junior ML Engineer Application",
            "cover_letter_body": "Hello, I built a defect detector ...",
            "sources": ["https://vision.example"],
            "evidence_item_ids": used_ids,
            "unsupported_claims": [],
        }
    )


NO_ANGLES = json.dumps({"angles": []})


def _draft_calls(llm):
    return [c for c in llm.calls if "Output plain text only" in (c["system"] or "")]


def test_selection_keeps_only_given_ids_and_always_a_cv(seeded):
    db, company, ids = seeded
    repo_a, repo_b = ids["owner/repo-0"], ids["owner/repo-3"]
    select = json.dumps({"item_ids": [repo_a, f"E{repo_b}", 999999], "why": {str(repo_a): "Vision repo."}})
    llm = SequencedLLMProvider([select, NO_ANGLES, _draft_json([repo_a, 999999, f"E{repo_b}"])])

    application = create_draft_application(db, llm, company)

    picked = [e["item_id"] for e in application.evidence]
    assert picked == [repo_a, repo_b, ids["data/cv.pdf"]]  # invented id dropped, a CV added
    assert {e["item_id"] for e in application.evidence if e["used"]} == {repo_a, repo_b}
    assert application.evidence[0]["why"] == "Vision repo."
    assert application.evidence[0]["source_url"] == "https://github.com/owner/repo-0"

    # The draft prompt carries exactly the picked records, and the company text is fenced.
    (draft_call,) = _draft_calls(llm)
    assert f"[E{repo_a}]" in draft_call["prompt"]
    assert f"[E{ids['owner/repo-1']}]" not in draft_call["prompt"]
    assert "<<<UNTRUSTED COMPANY CONTENT>>>" in llm.calls[0]["prompt"]
    # selection is structured; the letter itself is plain text (roadmap phase 4)
    assert llm.calls[0]["json_schema"] is not None and draft_call["json_schema"] is None


def test_broken_selection_falls_back_and_still_drafts(seeded):
    db, company, ids = seeded
    llm = SequencedLLMProvider(["not json at all", NO_ANGLES, _draft_json([])])

    application = create_draft_application(db, llm, company)

    assert {e["item_id"] for e in application.evidence} == {ids["data/cv.pdf"], ids["data/linkedin.pdf"]}
    assert application.revision_history[0]["evidence_fallback"] is True
    assert "valid JSON" in application.revision_history[0]["evidence_error"]
    assert application.cover_letter_body


def test_regenerate_reuses_the_pick_until_the_owner_excludes_one(seeded):
    db, company, ids = seeded
    repo_a, repo_b = ids["owner/repo-0"], ids["owner/repo-1"]
    llm = SequencedLLMProvider([json.dumps({"item_ids": [repo_a, repo_b]}), NO_ANGLES, _draft_json([repo_a])])
    application = create_draft_application(db, llm, company)

    # Plain feedback: one LLM call (the draft), same records.
    llm2 = SequencedLLMProvider([_draft_json([repo_a])])
    regenerate_draft(db, llm2, application, feedback="Shorter.")
    assert len(llm2.calls) == 1
    assert [e["item_id"] for e in application.evidence][:2] == [repo_a, repo_b]

    # Owner excludes repo_a through the review page -> next regenerate picks again without it.
    r = client.post(f"/applications/{application.id}/evidence/{repo_a}/toggle", follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    application = db.get(Application, application.id)
    assert application.excluded_evidence_ids == [repo_a]

    llm3 = SequencedLLMProvider([json.dumps({"item_ids": [repo_a, repo_b]}), _draft_json([repo_b])])
    regenerate_draft(db, llm3, application, feedback="Use another project.")
    assert len(llm3.calls) == 2
    assert f"E{repo_a} " not in llm3.calls[0]["prompt"]  # not even offered
    assert repo_a not in [e["item_id"] for e in application.evidence]


def test_review_page_shows_the_evidence(seeded):
    db, company, ids = seeded
    repo_a = ids["owner/repo-0"]
    llm = SequencedLLMProvider([json.dumps({"item_ids": [repo_a]}), NO_ANGLES, _draft_json([repo_a])])
    application = create_draft_application(db, llm, company)

    page = client.get(f"/applications/{application.id}").text
    assert 'id="evidence"' in page
    assert "owner/repo-0" in page
    assert f"/applications/{application.id}/evidence/{repo_a}/toggle" in page


def test_a_source_ingested_twice_is_offered_once(seeded):
    db, company, ids = seeded
    dup = ProfileItem(
        source_type="github_repo",
        source_ref="owner/repo-0",
        extracted={"purpose": "Repo 0, newer", "stack": ["Python"], "role": "author", "highlights": []},
        status=ProfileItemStatus.ANALYZED,
    )
    db.add(dup)
    db.commit()
    try:
        refs = [(i.source_type, i.source_ref) for i in evidence_mod.candidate_items(db)]
        assert refs.count(("github_repo", "owner/repo-0")) == 1
        assert dup.id in [i.id for i in evidence_mod.candidate_items(db)]  # the newest copy wins
    finally:
        db.delete(dup)
        db.commit()


def test_selection_reasons_list_shape(seeded):
    """The structured-output shape: reasons as a list of {item_id, why}."""
    db, company, ids = seeded
    repo_a = ids["owner/repo-0"]
    select = json.dumps({"item_ids": [repo_a], "reasons": [{"item_id": repo_a, "why": "Same domain."}, {"item_id": 999999, "why": "x"}]})
    llm = SequencedLLMProvider([select, NO_ANGLES, _draft_json([repo_a])])

    application = create_draft_application(db, llm, company)

    assert application.evidence[0]["why"] == "Same domain."


def test_pre_rank_matches_turkish_company_text_to_english_records():
    """Phase 1 item 7 / section 8: the code pre-rank keeps the ~20 most relevant
    records before any model call, across languages."""
    from app.applications.evidence import pre_rank
    from app.models import ProfileItem

    def repo(i, one_liner, domains=()):
        return ProfileItem(id=10_000 + i, source_type="github_repo", source_ref=f"o/r{i}",
                           extracted={"one_liner": one_liner, "domains": list(domains)}, facts={"stack": ["Python"]})

    noise = [repo(i, f"A todo list app number {i}") for i in range(30)]
    target = repo(99, "Segments skin lesions with a U-Net", ["medical imaging", "computer vision"])
    cv_entry = ProfileItem(id=20_000, source_type="cv", source_ref="cv#x", extracted={"entry_type": "experience", "role": "Intern"})
    kept = pre_rank(noise + [target, cv_entry], "Bilgisayarlı görü ve tıbbi görüntüleme alanında çalışan bir ekip", limit=5)
    assert target in kept and cv_entry in kept and len(kept) == 5


def test_prompt_section_gives_the_role_angle_results_and_owner_facts():
    from app.applications.evidence import evidence_prompt_section
    from app.models import ProfileItem

    item = ProfileItem(
        id=30_000, source_type="github_repo", source_ref="o/lesion", source_urls=["https://github.com/o/lesion"],
        extracted={
            "one_liner": "Lesion segmentation", "role_angles": {"computer_vision": "Built the segmentation model.", "backend_data": "Wrote the API."},
            "results": [{"claim": "Dice 0.87", "quote": "Dice score of 0.87", "source": "readme"}],
            "talking_points": ["Dice loss for imbalance"], "maturity": {"tests": True},
        },
        facts={"stack": ["PyTorch", "FastAPI"]},
        owner_facts=[{"question": "Solo?", "answer": "Solo project", "source": "owner"}],
    )
    text = evidence_prompt_section([item], "computer_vision")
    assert "ANGLE FOR THIS ROLE (computer_vision): Built the segmentation model." in text
    assert "Wrote the API." not in text
    assert 'RESULT: Dice 0.87 (quote: "Dice score of 0.87")' in text
    assert "OWNER STATED: Solo? -> Solo project" in text
    assert "STACK (from code): PyTorch, FastAPI" in text
