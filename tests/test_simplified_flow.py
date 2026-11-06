"""The simplified flow: irrelevant postings filtered by code, bullets sized by
project priority and blocked until they pass the ATS rules, specific tools kept
off CVs unless a posting asks, deleted repos stay hidden, and the profile page
says what to do next."""
import json

from fastapi.testclient import TestClient

from app.cv_builder.bank import BULLETS_FOR_PRIORITY, ats_issues, can_approve, generate_bullets, suggest_projects
from app.cv_builder.skills import default_tier
from app.db import SessionLocal, init_db
from app.main import app
from app.models import Bullet, CVProject, JobPosting, OwnerSettings, PostingStatus, ProfileItem, ProfileItemStatus
from app.postings.gates import gates_block, role_gate, run_gates
from app.web.routes.profile import item_title, repo_status
from tests.fakes import FakeLLMProvider

TARGETS = ["ML Engineer", "Computer Vision Engineer"]


def setup_module():
    """Pages need a CV on file (the onboarding gate sends you to /cvs otherwise)."""
    from app.models import CV
    from tests.conftest import make_cv_file

    init_db()
    db = SessionLocal()
    if not db.query(CV).filter(CV.file_path != "").count():
        db.add(CV(name="Simp CV", file_path=make_cv_file("simp.pdf")))
        db.commit()
    db.close()


def test_role_gate_rejects_clearly_unrelated_postings_only():
    for title in ("Sales Manager", "Muhasebe Uzmanı", "Customer Success Specialist", "Satış Temsilcisi", "İnsan Kaynakları Uzmanı"):
        assert role_gate(title, "", TARGETS, ["Python"])["result"] == "reject", title
    for title in ("Junior ML Engineer", "Bilgisayarlı Görü Mühendisi", "Backend Developer", "Data Analyst"):
        assert role_gate(title, "", TARGETS, ["Python"])["result"] == "pass", title
    # a non-technical word but a technical role -> kept
    assert role_gate("Sales Engineer (Python)", "", TARGETS, ["Python"])["result"] == "pass"
    # unclear -> shown, never auto-dropped
    assert role_gate("Product Designer", "", TARGETS, [])["result"] == "ambiguous"
    assert gates_block(run_gates("Sales Manager", "Ankara", "", TARGETS, []))
    assert not gates_block(run_gates("Product Designer", "Ankara", "", TARGETS, []))


def test_ats_rules_for_bullets():
    assert ats_issues("Built a U-Net pipeline estimating EF from echo video (Dice 0.931 on CAMUS).") == []
    assert set(ats_issues("I built the backend")) == {"weak_opener", "first_person"}
    assert ats_issues("Responsible for the API") == ["weak_opener"]
    assert "odd_characters" in ats_issues("Built X | Y")
    assert "too_long" in ats_issues("Built " + "a very long thing " * 20)
    blocked = Bullet(text="I built x", flags={"ats": ["first_person"]})
    assert not can_approve(blocked)
    assert can_approve(Bullet(text="Built x", flags={"ats": []}))


def test_specific_tools_are_not_core_by_default():
    assert default_tier("Python", "languages", [{"kind": "github_repo"}]) == "core"
    assert default_tier("Google AI Studio", "tools", [{"kind": "github_repo"}]) == "specific"


def test_priority_sets_bullet_count_and_small_work_is_off():
    init_db()
    db = SessionLocal()
    try:
        big = ProfileItem(source_type="github_repo", source_ref="simp/big", status=ProfileItemStatus.ANALYZED,
                          extracted={"one_liner": "Segmentation", "results": [{"claim": "Dice 0.9", "quote": "Dice 0.9", "source": "readme"}],
                                     "maturity": {"tests": True, "ci": True}},
                          facts={"stack": ["Python", "PyTorch"], "activity": {"stars": 5, "commits": 120}})
        tiny = ProfileItem(source_type="github_repo", source_ref="simp/tiny", status=ProfileItemStatus.ANALYZED, extracted={"one_liner": "hello"}, facts={})
        db.add_all([big, tiny])
        db.commit()
        suggest_projects(db)
        tiny_project = db.query(CVProject).filter(CVProject.profile_item_id == tiny.id).one()
        assert not tiny_project.selected and tiny_project.priority == "low"

        project = db.query(CVProject).filter(CVProject.profile_item_id == big.id).one()
        project.priority = "low"
        db.commit()
        answer = {"bullets": [{"text": f"Built part {n} with PyTorch.", "keywords": [], "role_types": [], "source_quote": "", "source_kind": "record", "metric": "none", "tech": ["PyTorch"], "domain": []} for n in range(3)]}
        llm = FakeLLMProvider(json.dumps(answer))
        generate_bullets(db, llm, project)
        assert "BULLETS WANTED: 1" in llm.calls[0]["prompt"]
        assert len(project.bullets) == BULLETS_FOR_PRIORITY["low"] == 1
    finally:
        db.rollback()
        for p in db.query(CVProject).filter(CVProject.name.in_(["big", "tiny"])).all():
            for b in p.bullets:
                db.delete(b)
            db.delete(p)
        db.query(ProfileItem).filter(ProfileItem.source_ref.like("simp/%")).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_approving_a_bullet_that_fails_ats_is_refused():
    init_db()
    db = SessionLocal()
    try:
        project = CVProject(name="ats-proj", selected=True)
        db.add(project)
        db.commit()
        bullet = Bullet(project_id=project.id, text="Built a thing", status="draft", flags={})
        db.add(bullet)
        db.commit()
        client = TestClient(app, headers={"Sec-Fetch-Site": "same-origin"})
        r = client.post(f"/cv-builder/bullets/{bullet.id}", data={"action": "approve", "text": "I was responsible for my thing"}, follow_redirects=False)
        assert r.status_code == 303, r.text[:500]
        db.refresh(bullet)
        assert bullet.status == "draft" and bullet.flags["ats"]
        client.post(f"/cv-builder/bullets/{bullet.id}", data={"action": "approve", "text": "Built a segmentation service in PyTorch."})
        db.refresh(bullet)
        assert bullet.status == "approved"
    finally:
        db.query(Bullet).filter(Bullet.project_id == project.id).delete()
        db.delete(project)
        db.commit()
        db.close()


def test_deleted_repo_stays_hidden_and_status_is_known():
    item = ProfileItem(source_type="github_repo", source_ref="me/r", status=ProfileItemStatus.ANALYZED, source_version={"pushed_at": "2026-01-01T00:00:00"})
    repo = {"full_name": "me/r", "pushed_at": "2026-01-01T00:00:00"}
    assert repo_status(repo, item, set()) == "unchanged"
    assert repo_status({**repo, "pushed_at": "2026-02-01T00:00:00"}, item, set()) == "changed"
    assert repo_status(repo, None, set()) == "new"
    assert repo_status(repo, None, {"me/r"}) == "excluded"

    init_db()
    db = SessionLocal()
    try:
        stored = ProfileItem(source_type="github_repo", source_ref="simp/deleted", status=ProfileItemStatus.ANALYZED, extracted={})
        db.add(stored)
        db.commit()
        stored_id = stored.id
        r = TestClient(app, headers={"Sec-Fetch-Site": "same-origin"}).post(f"/profile/items/{stored_id}/delete", follow_redirects=False)
        assert r.status_code == 303, r.text[:500]
        db.expire_all()
        settings = db.query(OwnerSettings).first()
        assert "simp/deleted" in (settings.excluded_repos or [])
        assert db.get(ProfileItem, stored_id) is None
    finally:
        settings = db.query(OwnerSettings).first()
        if settings is not None:
            settings.excluded_repos = [r for r in settings.excluded_repos or [] if r != "simp/deleted"]
            db.commit()
        db.close()


def test_readable_record_titles():
    assert item_title(ProfileItem(source_type="github_repo", source_ref="me/echo-ef", extracted={})) == "echo-ef"
    entry = ProfileItem(source_type="cv", source_ref="cv.pdf#3", extracted={"entry_type": "experience", "role": "Data Intern", "start": "07/2025", "end": "09/2025"})
    assert item_title(entry) == "Data Intern · 07/2025 – 09/2025"


def test_postings_page_folds_irrelevant_ones():
    init_db()
    db = SessionLocal()
    try:
        relevant = JobPosting(title="Simp ML Engineer", url="https://x.example/1", description_text="", status=PostingStatus.NEW,
                              gates={"role": {"result": "pass"}})
        unrelated = JobPosting(title="Simp Sales Manager", url="https://x.example/2", description_text="", status=PostingStatus.NEW,
                               gates={"role": {"result": "reject"}})
        db.add_all([relevant, unrelated])
        db.commit()
        client = TestClient(app, headers={"Sec-Fetch-Site": "same-origin"})
        page = client.get("/postings?seg=kept").text
        assert "Simp ML Engineer" in page and "Simp Sales Manager" not in page
        assert "Simp Sales Manager" in client.get("/postings?seg=kept&show_all=1").text
        client.post("/postings/bulk", data={"action": "dismiss_hidden", "seg": "kept"})
        db.refresh(unrelated)
        db.refresh(relevant)
        assert unrelated.status == PostingStatus.DISMISSED and relevant.status == PostingStatus.NEW
    finally:
        db.query(JobPosting).filter(JobPosting.title.like("Simp %")).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_today_list_orders_by_urgency_and_caps_each_kind():
    from types import SimpleNamespace as NS

    from app.web.routes.dashboard import PER_KIND, today_list

    company = NS(id=1, name="Co")
    review = [NS(id=i, company=company, target_position="ML") for i in range(PER_KIND + 2)]
    posting = NS(id=5, title="ML Engineer", company=company, company_name="", fit={"verdict": "strong"})
    todo = today_list(review, [], [posting], [company], [], [])
    assert [g["kind"] for g in todo] == ["review", "posting", "draft"]
    assert len(todo[0]["rows"]) == PER_KIND and todo[0]["more"] == 2
    assert todo[2]["rows"][0]["action"] == "/companies/1/draft"
    with_inbox = today_list([], [], [], [], [], [], inbox_count=4)
    assert with_inbox[0]["kind"] == "inbox" and with_inbox[0]["count"] == 4


def test_found_postings_wait_in_the_inbox_until_kept():
    init_db()
    db = SessionLocal()
    try:
        found = JobPosting(title="Simp Inbox CV Engineer", url="https://x.example/i1", location="Ankara", description_text="", status=PostingStatus.NEW,
                           source="ats_api", inbox=True, gates={"role": {"result": "pass"}})
        other = JobPosting(title="Simp Inbox Data Engineer", url="https://x.example/i2", location="Ankara", description_text="", status=PostingStatus.NEW,
                           source="ats_api", inbox=True, gates={"role": {"result": "pass"}})
        db.add_all([found, other])
        db.commit()
        client = TestClient(app)
        assert "Simp Inbox CV Engineer" in client.get("/postings?seg=new").text
        assert "Simp Inbox CV Engineer" not in client.get("/postings?seg=kept").text
        client.post(f"/postings/{found.id}/triage", data={"action": "keep"})
        client.post(f"/postings/{other.id}/triage", data={"action": "dismiss"})
        db.expire_all()
        assert found.inbox is False and found.status == PostingStatus.NEW
        assert other.status == PostingStatus.DISMISSED
        assert "Simp Inbox CV Engineer" in client.get("/postings?seg=kept").text
    finally:
        db.query(JobPosting).filter(JobPosting.title.like("Simp Inbox %")).delete(synchronize_session=False)
        db.commit()
        db.close()
