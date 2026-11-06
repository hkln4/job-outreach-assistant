"""Phase 0 research fixes: emails found in raw HTML (footer mailto, Cloudflare,
"[at]"), job pages kept by the budgeted context, preference evidence pointing
at the page the snippet is on, and deterministic form/ATS detection."""
import json

import httpx

import app.companies.fetch as fetch
import app.companies.workflow as workflow
from app.applications.drafting import draft_application
from app.channels.detect import detect_channels
from app.channels.domains import host_matches, registrable_domain
from app.companies.context import build_company_context, locate_quote
from app.companies.emails import extract_emails
from app.db import SessionLocal, init_db
from app.models import CV, Company, Preference, PreferenceType, Profile
from tests.fakes import FakeLLMProvider, SequencedLLMProvider

ARTICLE = "<p>" + "We build route optimization software for logistics fleets across Turkey. " * 30 + "</p>"


def setup_module():
    init_db()


def _cf_encode(address: str, key: int = 0x42) -> str:
    return f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in address)


# --- Bug 1: emails ---


def test_extract_emails_reads_mailto_cloudflare_and_obfuscated_addresses():
    html = f"""<html><body><article>{ARTICLE}</article>
      <footer>
        <a href="mailto:IK@Acme.example?subject=Basvuru">İnsan Kaynakları</a>
        <p>Destek: <span class="__cf_email__" data-cfemail="{_cf_encode('destek@acme.example')}">[email&#160;protected]</span></p>
        <p>Kariyer: kariyer [at] acme [dot] example</p>
        <img src="logo@2x.png">
      </footer></body></html>"""
    found = {e["email"]: e for e in extract_emails(html, "https://acme.example/")}
    assert set(found) == {"ik@acme.example", "destek@acme.example", "kariyer@acme.example"}
    assert found["ik@acme.example"]["context"] == "İnsan Kaynakları"
    assert all(e["url"] == "https://acme.example/" for e in found.values())


def _mock_site(monkeypatch, pages: dict[str, str]):
    def handler(request: httpx.Request) -> httpx.Response:
        html = pages.get(request.url.path)
        return httpx.Response(404, text="not found") if html is None else httpx.Response(200, text=html)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(fetch.httpx, "Client", lambda **kw: real_client(transport=transport, **kw))


def test_footer_only_email_reaches_research_emails(monkeypatch):
    # A long article (readability keeps only it) whose only address is a
    # footer mailto: the LLM never sees it, code must still find it.
    _mock_site(
        monkeypatch,
        {
            "/": f'<html><body><article>{ARTICLE}</article><footer><a href="mailto:hr@acme.example">HR</a> '
            f'<span data-cfemail="{_cf_encode("info@acme.example")}">[email protected]</span> '
            "jobs(at)acme.example</footer></body></html>"
        },
    )
    db = SessionLocal()
    try:
        company = Company(name="Footer Co", website_url="https://acme.example/")
        db.add(company)
        db.commit()
        llm = SequencedLLMProvider([json.dumps({"summary": "Route optimization.", "emails": []})])
        workflow.research_company(db, llm, company)
        emails = {e["email"]: e for e in company.research["emails"]}
        assert {"hr@acme.example", "info@acme.example", "jobs@acme.example"} <= set(emails)
        assert emails["hr@acme.example"]["found_by"] == "page"
        assert company.contact_email == "hr@acme.example"  # HR-first still applies
        # the footer block is appended so the model sees the context too
        assert "[footer/contact]" in company.research["pages"][0]["text"]
    finally:
        db.close()


# --- Bug 2: budgeted context ---


def _pages_with_jobs() -> list[dict]:
    core = [
        {"url": "https://acme.example/", "kind": "home", "text": "HOME " + "h" * 6000},
        {"url": "https://acme.example/careers", "kind": "careers", "text": "CAREERS " + "c" * 6000},
        {"url": "https://acme.example/about", "kind": "about", "text": "ABOUT " + "a" * 6000},
        {"url": "https://acme.example/contact", "kind": "contact", "text": "CONTACT " + "k" * 6000},
    ]
    jobs = [
        {"url": "https://acme.example/jobs/1", "kind": "job", "text": "JOBONE requirements: Python, PyTorch. " + "j" * 6000},
        {"url": "https://acme.example/jobs/2", "kind": "job", "text": "JOBTWO requirements: savunma projeleri. " + "j" * 6000},
    ]
    return core + jobs


def test_build_company_context_keeps_every_page_and_puts_jobs_first():
    text = build_company_context(_pages_with_jobs(), 24000)
    assert len(text) <= 24000 + 200  # trim markers only
    for marker in ("JOBONE", "JOBTWO", "CAREERS", "ABOUT", "HOME", "CONTACT"):
        assert marker in text
    assert text.index("JOBONE") < text.index("CAREERS") < text.index("HOME") < text.index("CONTACT")


def test_job_pages_reach_the_drafting_and_preference_prompts(monkeypatch):
    pages = _pages_with_jobs()
    monkeypatch.setattr(
        workflow, "fetch_company_research", lambda url, careers_url=None: {"pages": pages, "content_chars": 42000}
    )
    db = SessionLocal()
    try:
        db.add(Preference(description="Avoid defense", type=PreferenceType.HARD, signals=["savunma"]))
        db.add_all([Profile(summary="ML engineer.", skills=[], target_roles=["ML Engineer"]), CV(name="ML CV", file_path="x.pdf")])
        company = Company(name="Budget Co", website_url="https://acme.example/")
        db.add(company)
        db.commit()

        llm = SequencedLLMProvider([json.dumps({"summary": "Fleet software."}), json.dumps({"flags": []})])
        workflow.research_company(db, llm, company)
        research_prompt, preference_prompt = llm.calls[0]["prompt"], llm.calls[1]["prompt"]
        for prompt in (research_prompt, preference_prompt):
            assert "JOBONE" in prompt and "JOBTWO" in prompt

        draft_llm = FakeLLMProvider(json.dumps({"subject": "S", "cover_letter_body": "B"}))
        draft_application(db, draft_llm, company)
        draft_prompt = draft_llm.calls[0]["prompt"]
        assert "JOBONE" in draft_prompt and "JOBTWO" in draft_prompt
        assert "RESEARCH SUMMARY: Fleet software." in draft_prompt
    finally:
        db.close()


# --- Bug 4: preference evidence on the right page ---


def test_preference_flag_points_at_the_page_with_the_snippet(monkeypatch):
    pages = [
        {"url": "https://acme.example/", "kind": "home", "text": "Fleet software for logistics. " * 20},
        {"url": "https://acme.example/jobs/9", "kind": "job", "text": "Görev: Savunma   projelerinde gömülü yazılım geliştirme. " * 5},
    ]
    monkeypatch.setattr(workflow, "fetch_company_research", lambda url, careers_url=None: {"pages": pages})
    db = SessionLocal()
    try:
        pref = Preference(description="Avoid defense", type=PreferenceType.HARD, signals=[])
        company = Company(name="Locate Co", website_url="https://acme.example/")
        db.add_all([pref, company])
        db.commit()
        flags = {
            "flags": [
                {"preference_id": pref.id, "evidence_snippet": "savunma projelerinde gömülü", "confidence": "high"},
                {"preference_id": pref.id, "evidence_snippet": "text the model made up", "confidence": "low"},
            ]
        }
        llm = SequencedLLMProvider([json.dumps({"summary": "x"}), json.dumps(flags)])
        workflow.research_company(db, llm, company)
        located, invented = company.preference_check
        assert located["source_url"] == "https://acme.example/jobs/9" and located["source_url_verified"] is True
        assert invented["source_url"] == "https://acme.example/" and invented["source_url_verified"] is False
    finally:
        db.close()


def test_locate_quote_accepts_a_shortened_quote():
    pages = [{"url": "u1", "text": "Nothing here."}, {"url": "u2", "text": "We deliver systems for the defense industry and more."}]
    assert locate_quote("systems for the defense industry ... more", pages) == "u2"
    assert locate_quote("not present anywhere", pages) is None


# --- Bug 5: channels ---


def test_detect_channels_finds_cv_form_ats_links_and_portals():
    html = """<html><body>
      <form action="/basvuru" method="post">
        <label for="n">Ad Soyad</label><input id="n" name="name" required>
        <label for="cv">Özgeçmiş</label><input id="cv" type="file" name="cv">
        <textarea name="note" maxlength="500"></textarea>
        <div class="g-recaptcha"></div>
      </form>
      <form action="/search"><input name="q"></form>
      <a href="https://jobs.lever.co/acme">Open roles</a>
      <iframe src="https://boards.greenhouse.io/embed/job_board?for=acme"></iframe>
      <a href="https://evil-greenhouse.io/acme">phish</a>
      <a href="https://www.kariyer.net/firma/acme/12">kariyer.net</a>
      <a href="https://www.workable.com/">Powered by Workable</a>
    </body></html>"""
    found = detect_channels(html, "https://acme.example/careers")
    by_kind = {}
    for entry in found:
        by_kind.setdefault(entry["kind"], []).append(entry)

    form = by_kind["site_form"][0]
    assert form["url"] == "https://acme.example/careers"
    schema = form["form_schema"]
    assert schema["has_file_input"] and schema["has_captcha"]
    assert {"label": "Özgeçmiş", "name": "cv", "type": "file", "required": False, "maxlength": None, "options": []} in schema["fields"]
    assert any(f["maxlength"] == 500 for f in schema["fields"])

    boards = {(b["provider"], b["token"]) for b in by_kind["ats_board"]}
    assert boards == {("lever", "acme"), ("greenhouse", "acme")}
    assert [p["provider"] for p in by_kind["portal"]] == ["kariyer.net"]
    assert len(found) == 4  # search form, look-alike host and vendor homepage ignored


def test_contact_form_is_told_apart_from_an_application_form():
    html = """<form><input type="email" name="email"><textarea name="mesaj"></textarea></form>"""
    (entry,) = detect_channels(html, "https://acme.example/iletisim")
    assert entry["kind"] == "contact_form"


def test_registrable_domain_handles_turkish_suffixes():
    assert registrable_domain("https://kariyer.acme.com.tr/x") == "acme.com.tr"
    assert registrable_domain("boards.greenhouse.io") == "greenhouse.io"
    assert host_matches("https://a.b.greenhouse.io", "greenhouse.io")
    assert not host_matches("https://greenhouse.io.example.com", "greenhouse.io")
