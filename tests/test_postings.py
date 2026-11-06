"""Roadmap phase 3: job postings - intake, dedup, gates, fit with verified
quotes, legitimacy, ATS boards with the Turkey filter, careers watch,
search-based board discovery, and posting-led drafting."""
import json

import httpx
from fastapi.testclient import TestClient

import app.postings.service as service
from app.applications.drafting import create_draft_application
from app.config import get_settings
from app.db import SessionLocal, init_db
from app.main import app
from app.models import CV, AtsBoard, Company, CompanyState, JobPosting, PostingStatus, Profile, ProfileItem, ProfileItemStatus
from app.postings import ats, careers_watch, search
from app.postings.evaluation import verdict_for
from app.postings.intake import json_ld_posting
from app.web.deps import get_draft_llm, get_llm, get_review_llm
from tests.conftest import make_cv_file
from tests.fakes import RoutedLLMProvider

POSTING_TEXT = """Junior Computer Vision Engineer - Ankara (Hybrid)
PV Unique Vision builds defect detection for factory lines.
Requirements:
- Python and PyTorch experience
- Experience with object detection models
Nice to have:
- Docker
En az 1 yıl deneyim tercih sebebidir. Apply at https://pv-unique-vision.example/careers/cv-engineer
"""

PARSED = json.dumps({
    "title": "Junior Computer Vision Engineer", "company_name": "PV Unique Vision", "location": "Ankara (Hybrid)",
    "work_mode": "hybrid", "requirements": ["Python and PyTorch experience", "Experience with object detection models", "10 years of Rust"],
    "nice_to_have": ["Docker"], "seniority": "junior", "deadline": "", "apply_url": "https://pv-unique-vision.example/careers/cv-engineer",
    "employment_type": "full-time",
})


def _fit(evidence_id: int) -> str:
    dim = lambda score, quote: {"score": score, "posting_quote": quote, "evidence_ids": [evidence_id, 999999], "note": "n"}  # noqa: E731
    return json.dumps({
        "scores": {"technical": dim(85, "Python and PyTorch experience"), "experience": dim(80, "En az 1 yıl deneyim"),
                   "domain": dim(75, "defect detection"), "career": dim(90, "invented quote not in posting")},
        "strengths": [{"point": "Has PyTorch", "posting_quote": "PyTorch"}],
        "gaps": [{"point": "No factory domain", "posting_quote": "factory lines"}],
    })


def setup_module():
    init_db()
    db = SessionLocal()
    try:
        if db.query(CV).first() is None:
            db.add(CV(name="Postings Gate CV", file_path=make_cv_file("postings-gate.pdf")))
        db.commit()
    finally:
        db.close()


def _evidence_item(db) -> ProfileItem:
    item = db.query(ProfileItem).filter(ProfileItem.source_ref == "owner/defect-det").first()
    if item is None:
        item = ProfileItem(source_type="github_repo", source_ref="owner/defect-det", status=ProfileItemStatus.ANALYZED,
                           extracted={"one_liner": "YOLO defect detection", "purpose": "x", "stack": ["PyTorch"]})
        db.add(item)
        db.commit()
    return item


def test_pasted_posting_is_parsed_gated_matched_evaluated_and_deduped():
    db = SessionLocal()
    try:
        company = Company(name="PV Unique Vision", website_url="https://pv-unique-vision.example", state=CompanyState.APPLICABLE)
        db.add(company)
        db.commit()
        item = _evidence_item(db)
        llm = RoutedLLMProvider({"parsing ONE job posting": PARSED, "judging how well a job posting fits": _fit(item.id)}, default=json.dumps({"flags": []}))

        posting, created = service.add_posting(db, llm, text=POSTING_TEXT, url="https://pv-unique-vision.example/careers/cv-engineer?utm_source=x")
        assert created and posting.company_id == company.id
        assert posting.url == "https://pv-unique-vision.example/careers/cv-engineer"
        assert posting.language == "en"
        assert posting.requirements == ["Python and PyTorch experience", "Experience with object detection models"]
        assert posting.extra["unverified_requirements"] == ["10 years of Rust"]
        assert posting.gates["location"]["result"] == "pass" and posting.gates["seniority"]["result"] == "pass"
        fit = posting.fit
        assert fit["scores"]["career"]["posting_quote"] == ""  # invented quote dropped
        assert fit["scores"]["technical"]["evidence_ids"] == [item.id] and 999999 in fit["dropped_evidence_ids"]
        assert fit["verdict"] == "strong"

        again, created_again = service.add_posting(db, llm, text=POSTING_TEXT + "\n")
        assert not created_again and again.id == posting.id  # same text -> same posting
    finally:
        db.close()


def test_gates_block_fit_and_verdict_is_computed_by_code():
    gates = {"location": {"result": "reject"}}
    assert verdict_for({"technical": {"score": 99}}, gates) == "blocked"
    assert verdict_for({d: {"score": 80} for d in ("technical", "experience", "domain", "career")}, {"seniority": {"result": "gap"}}) == "possible"

    db = SessionLocal()
    try:
        llm = RoutedLLMProvider({"parsing ONE job posting": PARSED.replace("Ankara (Hybrid)", "Remote - US")}, default=json.dumps({"flags": []}))
        posting, _ = service.add_posting(db, llm, text=POSTING_TEXT.replace("Ankara (Hybrid)", "Remote - US") + " US only.")
        assert posting.gates["location"]["result"] == "reject"
        assert llm.calls_for("judging how well a job posting fits") == []  # no fit call for a blocked posting
        assert any(f["kind"] == "no_company" for f in posting.legitimacy_flags) is False  # it names PV Unique Vision
    finally:
        db.close()


def test_json_ld_fields_are_read_by_code():
    html = """<script type="application/ld+json">{"@context": "https://schema.org", "@graph": [{"@type": "JobPosting",
      "title": "Data Engineer", "datePosted": "2026-01-02", "validThrough": "2026-12-31T00:00",
      "hiringOrganization": {"name": "Beta Veri", "sameAs": "https://beta.example"},
      "jobLocation": {"address": {"addressLocality": "İstanbul", "addressCountry": "TR"}},
      "description": "<p>Build ETL pipelines with Airflow.</p>"}]}</script>"""
    ld = json_ld_posting(html)
    assert ld["title"] == "Data Engineer" and ld["company_name"] == "Beta Veri" and ld["location"] == "İstanbul, TR"
    assert ld["deadline"] == "2026-12-31" and "Airflow" in ld["description"]


def _board_client(payloads: dict[str, object]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        for key, payload in payloads.items():
            if key in str(request.url):
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_ats_board_turkey_filter_imports_accepted_and_holds_ambiguous():
    greenhouse = {"jobs": [
        {"id": 1, "title": "ML Engineer", "location": {"name": "Istanbul, Türkiye"}, "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "content": "&lt;p&gt;PyTorch&lt;/p&gt;", "updated_at": "2026-10-01"},
        {"id": 2, "title": "Data Scientist", "location": {"name": "EMEA"}, "absolute_url": "https://boards.greenhouse.io/acme/jobs/2", "content": "", "updated_at": ""},
        {"id": 3, "title": "Backend", "location": {"name": "Remote - US"}, "absolute_url": "https://boards.greenhouse.io/acme/jobs/3", "content": "", "updated_at": ""},
    ]}
    lever = [{"id": "x1", "text": "CV Intern", "categories": {"location": "Ankara"}, "hostedUrl": "https://jobs.lever.co/beta/x1", "applyUrl": "", "descriptionPlain": "OpenCV", "lists": [], "createdAt": 1767225600000}]
    client = _board_client({"boards-api.greenhouse.io/v1/boards/acme": greenhouse, "api.lever.co/v0/postings/beta": lever})
    postings = ats.read_board("greenhouse", "acme", client)
    assert [p["location_gate"]["result"] for p in postings] == ["pass", "ambiguous", "reject"]
    assert ats.read_board("lever", "beta", client)[0]["posted"] == "2026-01-01"

    db = SessionLocal()
    try:
        board = AtsBoard(provider="greenhouse", token="acme", company_name="Acme")
        db.add(board)
        db.commit()
        rules = {"home_city": "Istanbul"}
        counts = ats.import_board_postings(db, board, postings, rules)
        # ML Engineer in the home city comes in; Data Scientist in "EMEA" (not
        # remote) and a US-only remote backend role are counted, not imported
        assert counts == {"read": 3, "imported": 1, "already_known": 0, "role": 0, "level": 0, "location": 2}
        counts = ats.import_board_postings(db, board, postings, rules)
        assert counts["imported"] == 0 and counts["already_known"] == 1
        imported = db.query(JobPosting).filter(JobPosting.url == "https://boards.greenhouse.io/acme/jobs/1").one()
        assert imported.source == "ats_api" and imported.extra["parsed"] is False
    finally:
        db.close()


def test_careers_watch_calls_the_llm_only_when_the_page_changed():
    pages = {"text": "<html><body><h1>Kariyer</h1><a href='/jobs/ml'>ML Mühendisi</a>" + "<p>Açık pozisyonlarımız aşağıda.</p>" * 20 + "</body></html>"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        return httpx.Response(200, text=pages["text"])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    listed = json.dumps({"postings": [{"title": "ML Mühendisi", "url": "https://watch.example/jobs/ml", "details": "Ankara"}]})
    db = SessionLocal()
    try:
        company = Company(name="Watch Co", website_url="https://watch.example", careers_url="https://watch.example/kariyer", state=CompanyState.APPLICABLE)
        db.add(company)
        db.commit()
        llm = RoutedLLMProvider({"List the job postings": listed})
        assert careers_watch.watch_company(db, llm, company, client) == {"status": "first", "new": 1}
        assert careers_watch.watch_company(db, llm, company, client) == {"status": "unchanged", "new": 0}
        assert len(llm.calls) == 1  # the unchanged page cost no LLM call
        pages["text"] = pages["text"].replace("Kariyer", "Kariyer 2")
        assert careers_watch.watch_company(db, llm, company, client)["status"] == "changed"
        assert len(llm.calls) == 2

        company.careers_url = "https://watch.example/private/jobs"
        db.commit()
        assert careers_watch.watch_company(db, llm, company, client)["status"] == "blocked"
    finally:
        db.close()


def test_search_finds_board_tokens_only_when_enabled(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "search_api_enabled", False)
    db = SessionLocal()
    try:
        try:
            search.discover_boards(db, ["q"])
            raise AssertionError("search must be off by default")
        except search.SearchNotConfigured:
            pass
        monkeypatch.setattr(settings, "search_api_enabled", True)
        monkeypatch.setattr(settings, "search_api_key", "test-key")
        sent = {}

        def tavily(request: httpx.Request) -> httpx.Response:
            sent.update(json.loads(request.content), auth=request.headers["authorization"])
            return httpx.Response(200, json={"results": [{"url": "https://jobs.lever.co/searchco/123"}, {"url": "https://jobs.ashbyhq.com/other"}, {"url": "https://news.example/x"}]})

        client = httpx.Client(transport=httpx.MockTransport(tavily))
        found = search.discover_boards(db, ["site:jobs.lever.co Istanbul"], client=client)
        assert sorted((b.provider, b.token) for b in found["new_boards"]) == [("ashby", "other"), ("lever", "searchco")]
        assert sent["include_domains"] == ["jobs.lever.co"] and sent["auth"] == "Bearer test-key"  # site: -> include_domains
        assert db.query(JobPosting).filter(JobPosting.url.like("%searchco%")).count() == 0  # tokens only, no postings
    finally:
        db.close()


def test_drafting_for_a_posting_leads_with_its_text_and_links_it():
    db = SessionLocal()
    try:
        if db.query(Profile).first() is None:
            db.add(Profile(summary="CV engineer.", skills=[], target_roles=["Computer Vision Engineer"]))
        company = Company(name="Unresearched Co", website_url="https://unresearched.example", state=CompanyState.APPLICABLE)
        db.add(company)
        db.commit()
        posting = JobPosting(company_id=company.id, title="Görüntü İşleme Mühendisi", url="https://unresearched.example/ilan/1",
                             description_text="Görüntü işleme alanında çalışacak mühendis arıyoruz. " * 10, language="tr", status=PostingStatus.NEW)
        db.add(posting)
        db.commit()
        draft = json.dumps({"subject": "Başvuru", "cover_letter_body": "Merhaba", "sources": ["https://unresearched.example/ilan/1"], "cv_choice_name": ""})
        llm = RoutedLLMProvider({"writing a job application email": draft, "choosing which of a job applicant": json.dumps({"item_ids": []})})
        application = create_draft_application(db, llm, company, job_posting=posting)
        prompt = llm.calls_for("writing a job application email")[0]["prompt"]
        assert "JOB POSTING BEING ANSWERED: Görüntü İşleme Mühendisi" in prompt
        assert "JOB POSTING LANGUAGE:\nTurkish" in prompt or "OWNER'S PREFERRED EMAIL LANGUAGE" in prompt
        assert application.job_posting_id == posting.id and application.target_position == "Görüntü İşleme Mühendisi"
        assert application.sources == ["https://unresearched.example/ilan/1"]  # the posting URL is a valid source

        posting.status = PostingStatus.EXPIRED
        db.commit()
        try:
            create_draft_application(db, llm, company, job_posting=posting)
            raise AssertionError("an expired posting must not be drafted")
        except ValueError as exc:
            assert "expired" in str(exc)
    finally:
        db.close()


def test_postings_pages_render_and_paste_flow_works(monkeypatch):
    llm = RoutedLLMProvider({"parsing ONE job posting": PARSED.replace("PV Unique Vision", "Web Flow Co")}, default=json.dumps({"flags": []}))
    app.dependency_overrides[get_llm] = lambda: llm
    try:
        client = TestClient(app)
        assert client.get("/postings").status_code == 200
        assert client.get("/postings/boards").status_code == 200
        response = client.post("/postings", data={"text": POSTING_TEXT.replace("PV Unique Vision", "Web Flow Co") + " unique-web-flow"})
        assert response.status_code == 200 and "Junior Computer Vision Engineer" in response.text
        assert "Web Flow Co" in response.text  # proposed company shown
        bad = client.post("/postings", data={"text": "", "url": ""})
        assert "Give the posting" in bad.text
    finally:
        app.dependency_overrides.pop(get_llm, None)
        app.dependency_overrides.pop(get_draft_llm, None)
        app.dependency_overrides.pop(get_review_llm, None)
