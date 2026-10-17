"""Company discovery: directories are read without guessing websites, imports
never duplicate or resurrect rejected companies, screening keeps only real
preference ids, and only owner-approved candidates become Companies - tagged,
and without spending a research call."""
import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

import app.discovery.sources as sources
import app.discovery.triage as triage
from app.companies.workflow import has_usable_research
from app.db import SessionLocal, init_db
from app.discovery import service
from app.main import app
from app.models import (
    CV,
    CandidateStatus,
    Company,
    CompanyCandidate,
    DiscoverySource,
    Preference,
    PreferenceType,
)
from app.web.deps import get_llm
from tests.fakes import FakeLLMProvider, SequencedLLMProvider

client = TestClient(app)

LONG_TEXT = (
    "We build computer vision software that inspects products on factory lines, written in Python. "
    "Our team in Ankara trains models and ships them to edge devices for manufacturers. " * 4
)


def setup_module():
    init_db()
    db = SessionLocal()
    try:
        if db.query(CV).first() is None:
            db.add(CV(name="Discovery CV", file_path="data/cv_variants/discovery.pdf"))
            db.commit()
    finally:
        db.close()


def teardown_module():
    app.dependency_overrides.pop(get_llm, None)


def _mock_http(monkeypatch, handler):
    real_client = httpx.Client

    def make_client(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(sources.httpx, "Client", make_client)


def _source(db, name="Test Teknokent", kind="odtu_json", url="https://test.example/companies.json"):
    source = DiscoverySource(name=name, city="Ankara", url=url, kind=kind)
    db.add(source)
    db.commit()
    return source


# --- readers ---


def test_odtu_reader_normalizes_urls_and_keeps_missing_ones_empty(monkeypatch):
    payload = [
        {"name": "3DTİM ELEKTRONİK ANONİM ŞİRKETİ", "url": "http://www.beevision.ai"},
        {"name": "Şirket Örnek A.Ş.", "url": "www.ornek.com.tr"},
        {"name": "Sitesiz Ltd.", "url": ""},
        {"name": "", "url": "https://noname.example"},
    ]
    _mock_http(monkeypatch, lambda request: httpx.Response(200, json=payload))

    entries = [
        {k: e[k] for k in ("name", "website_url")} for e in sources.read_odtu_json("https://test.example/companies.json")
    ]

    assert entries == [
        {"name": "3DTİM ELEKTRONİK ANONİM ŞİRKETİ", "website_url": "http://www.beevision.ai"},
        {"name": "Şirket Örnek A.Ş.", "website_url": "https://www.ornek.com.tr"},
        {"name": "Sitesiz Ltd.", "website_url": None},
    ]


def test_html_reader_only_keeps_websites_linked_on_the_page(monkeypatch):
    pages = {
        "/firmalar": (
            "<html><body><h1>Firmalar</h1><p>" + "Teknokent firma rehberi. " * 20 + "</p>"
            "<a href='https://alpha.example'>Alpha Yazılım</a> Beta Elektronik"
            "<a href='/firmalar?page=2'>2</a></body></html>"
        ),
        "/firmalar?page=2": "<html><body><p>Gamma Robotik</p><a href='https://gamma.example'>Gamma</a></body></html>",
    }

    def handler(request):
        key = request.url.raw_path.decode()
        return httpx.Response(200, html=pages.get(key, "<html></html>"))

    _mock_http(monkeypatch, handler)
    llm = SequencedLLMProvider(
        [
            json.dumps(
                {
                    "companies": [
                        {"name": "Alpha Yazılım", "website": "https://alpha.example"},
                        {"name": "Beta Elektronik", "website": "https://beta-guessed.example"},
                    ]
                }
            ),
            json.dumps({"companies": [{"name": "Gamma Robotik", "website": "https://gamma.example/"}]}),
        ]
    )

    entries = [{k: e[k] for k in ("name", "website_url")} for e in sources.read_html_directory(llm, "https://park.example/firmalar")]

    assert entries == [
        {"name": "Alpha Yazılım", "website_url": "https://alpha.example"},
        {"name": "Beta Elektronik", "website_url": None},  # not linked on the page -> not guessed
        {"name": "Gamma Robotik", "website_url": "https://gamma.example"},
    ]
    assert len(llm.calls) == 2  # pagination followed
    assert "<<<UNTRUSTED DIRECTORY PAGE>>>" in llm.calls[0]["prompt"]


def test_html_reader_says_so_when_the_list_is_javascript_only(monkeypatch):
    _mock_http(monkeypatch, lambda request: httpx.Response(200, html="<html><body><div id='list'></div></body></html>"))
    with pytest.raises(sources.DirectoryReadError, match="JavaScript"):
        sources.read_html_directory(FakeLLMProvider("{}"), "https://park.example/js-list")


CHROME = "<footer><a href='https://agency.example'>Web by Agency</a> <a href='mailto:info@park.example'>park</a></footer>"


def test_cyberpark_reader_walks_pages_until_empty(monkeypatch):
    card = (
        "<div class='col-md-3'><a target='_blank' {href}><div><h3 class='title'>{name}</h3>"
        "<p>{sector}</p></div></a></div>"
    )
    pages = {
        "/firma-arsiv": card.format(href="href='http://alpha.example'", name="ALPHA A.Ş.", sector="Yazılım")
        + card.format(href="", name="BETA LTD.", sector="Savunma"),
        "/firma-arsiv/2": card.format(href="href='gamma.example'", name="GAMMA A.Ş.", sector=""),
    }
    _mock_http(monkeypatch, lambda r: httpx.Response(200, html=pages.get(r.url.path, "<html></html>")))

    entries = sources.read_cyberpark("https://park.example/firma-arsiv")

    assert [(e["name"], e["website_url"], e["sector"]) for e in entries] == [
        ("ALPHA A.Ş.", "http://alpha.example", "Yazılım"),
        ("BETA LTD.", None, "Savunma"),
        ("GAMMA A.Ş.", "https://gamma.example", ""),
    ]


def test_hacettepe_reader_reads_categories_and_contact_block(monkeypatch):
    companies = {f"/tr/firma/c{i}": (f"FIRMA {i} A.Ş.", f"http://www.firma{i}.example", f"info@firma{i}.example") for i in range(4)}
    category = "".join(f"<div class='firma_adi'><a href='{path}'>{n}</a></div>" for path, (n, _, _) in companies.items())

    def handler(request):
        path = request.url.path
        if path == "/tr/firma_rehberi":
            return httpx.Response(200, html="<a href='/tr/firma_rehberi/yazilim-29'>YAZILIM</a>")
        if path == "/tr/firma_rehberi/yazilim-29":
            return httpx.Response(200, html=category)
        name, site, mail = companies[path]
        return httpx.Response(
            200,
            html=f"<main><div class='firma_detay_baslik'>{name}</div>"
            f"<div class='firma_detay_bilgi'>{site}</div><div class='firma_detay_bilgi'>{mail}</div>"
            f"<div class='aciklama'><h3>Vizyonumuz:</h3> https://unrelated.example</div></main>{CHROME}",
        )

    _mock_http(monkeypatch, handler)
    entries = sources.read_hacettepe("https://park.example/tr/firma_rehberi")

    first = entries[0]
    assert first["name"] == "FIRMA 0 A.Ş." and first["website_url"] == "http://www.firma0.example"
    assert first["contact_email"] == "info@firma0.example" and first["sector"] == "Yazılım"
    assert len(entries) == 4


def test_ostim_reader_decodes_emails_and_drops_site_chrome(monkeypatch):
    def cf(email, key=0x42):
        return f"{key:02x}" + "".join(f"{ord(ch) ^ key:02x}" for ch in email)

    names = ["ALFA", "BRAVO", "CHARLIE", "DELTA"]
    index = "".join(f"<a href='/tr/company/firmalar-119/{n.lower()}'>{n}</a>" for n in names)

    def handler(request):
        if request.url.path == "/tr/firmalar-119":
            return httpx.Response(200, html=index)
        n = request.url.path.rsplit("/", 1)[-1]
        site = "" if n == "alfa" else f"<a href='https://{n}.example'>web</a>"
        return httpx.Response(
            200,
            html=f"<h1>{n.upper()} A.Ş.</h1>{site}<a href='https://team@{n}.com'>x</a>"
            f"<span class='__cf_email__' data-cfemail='{cf(f'info@{n}.example')}'>[email protected]</span>{CHROME}",
        )

    _mock_http(monkeypatch, handler)
    entries = {e["name"]: e for e in sources.read_ostim("https://park.example/tr/firmalar-119")}

    assert entries["ALFA A.Ş."]["website_url"] is None  # the footer agency isn't its website
    assert entries["BRAVO A.Ş."]["website_url"] == "https://bravo.example"
    assert entries["BRAVO A.Ş."]["contact_email"] == "info@bravo.example"  # not the park's info@


def test_ankara_reader_follows_the_sitemap(monkeypatch):
    def handler(request):
        if request.url.path.endswith(".xml"):
            return httpx.Response(200, text="<urlset><url><loc>https://park.example/firma/one/</loc></url></urlset>")
        return httpx.Response(200, html="<h1>OneTech</h1><a href='https://onetech.example/'>site</a>")

    _mock_http(monkeypatch, handler)
    (entry,) = sources.read_ankara_teknokent("https://park.example/sitemap.xml")
    assert entry["name"] == "OneTech" and entry["website_url"] == "https://onetech.example/"


# --- import ---


def test_import_skips_known_companies_fills_their_tags_and_never_duplicates(monkeypatch):
    db = SessionLocal()
    try:
        source = _source(db)
        tracked = Company(name="Tracked Co", website_url="https://tracked.example")
        tagged = Company(name="Tagged Co", website_url="https://tagged.example", location="İstanbul")
        db.add_all([tracked, tagged])
        rejected = CompanyCandidate(
            source_id=source.id, name="Nope Ltd", website_url="https://nope.example", status=CandidateStatus.REJECTED
        )
        db.add(rejected)
        db.commit()

        entries = [
            {"name": "TRACKED LEGAL A.Ş.", "website_url": "http://www.tracked.example"},  # same domain
            {"name": "Tagged Co", "website_url": None},  # same name
            {"name": "Nope Ltd", "website_url": "https://nope.example"},  # rejected before
            {"name": "Fresh Co", "website_url": "https://fresh.example"},
        ]
        monkeypatch.setattr(service, "read_source", lambda llm, kind, url: entries)

        counts = service.import_source(db, None, source)
        assert counts == {"found": 4, "new": 1, "already_in_list": 2, "known": 1}

        db.refresh(tracked)
        db.refresh(tagged)
        assert (tracked.location, tracked.technopark) == ("Ankara", "Test Teknokent")
        assert tagged.location == "İstanbul"  # owner input is never overwritten
        assert tagged.technopark == "Test Teknokent"
        assert db.get(CompanyCandidate, rejected.id).status == CandidateStatus.REJECTED

        again = service.import_source(db, None, source)
        assert again["new"] == 0
        assert db.query(CompanyCandidate).filter(CompanyCandidate.name == "Fresh Co").count() == 1
    finally:
        db.close()


def test_reconcile_hides_candidates_already_in_the_list():
    db = SessionLocal()
    try:
        source = _source(db, name="Reconcile Park")
        company = Company(name="Reconcile Vision", website_url="https://www.reconcilevision.com.tr")
        db.add(company)
        db.commit()
        sister = CompanyCandidate(
            source_id=source.id, name="RV ELEKTRONİK A.Ş.", website_url="http://reconcilevision.ai",
            city="Ankara", technopark="Reconcile Park", contact_email="hr@reconcilevision.ai",
        )
        unrelated = CompanyCandidate(source_id=source.id, name="Totally Other A.Ş.", website_url="https://other-rc.example")
        db.add_all([sister, unrelated])
        db.commit()

        assert service.reconcile_pending(db) >= 1
        db.refresh(sister)
        db.refresh(unrelated)
        db.refresh(company)
        assert sister.status == CandidateStatus.MATCHED and sister.company_id == company.id
        assert unrelated.status == CandidateStatus.PENDING
        assert company.technopark == "Reconcile Park" and company.contact_email == "hr@reconcilevision.ai"
    finally:
        db.close()


# --- screening ---


def _candidate(db, website="https://screen.example", name=None):
    # A unique name per candidate: an approved test candidate becomes a Company,
    # and later candidates with the same name would (rightly) be reconciled away.
    source = _source(db, name="Screen Park")
    candidate = CompanyCandidate(
        source_id=source.id,
        name=name or f"SCREEN LEGAL {uuid.uuid4().hex[:6].upper()} A.Ş.",
        website_url=website,
        city="Ankara",
        technopark="Screen Park",
    )
    db.add(candidate)
    db.commit()
    return candidate


def test_screening_keeps_real_preference_ids_and_detects_careers(monkeypatch):
    db = SessionLocal()
    try:
        pref = Preference(description="avoid defence industry", type=PreferenceType.HARD, signals=[])
        db.add(pref)
        db.commit()
        candidate = _candidate(db)
        html = "<html><body><a href='/kariyer'>Kariyer</a></body></html>"
        monkeypatch.setattr(triage, "_fetch_homepage", lambda url: (html, LONG_TEXT))
        llm = FakeLLMProvider(
            json.dumps(
                {
                    "brand_name": "ScreenVision",
                    "summary": "Builds camera-based inspection software for factories.",
                    "sector": "computer vision software",
                    "fit": "high",
                    "fit_reason": "Python computer vision work.",
                    "flags": [
                        {"preference_id": pref.id, "evidence_snippet": "defence customers"},
                        {"preference_id": 999999, "evidence_snippet": "made up"},
                    ],
                }
            )
        )

        triage.triage_candidate(db, llm, candidate)

        result = candidate.triage
        assert result["brand_name"] == "ScreenVision" and result["fit"] == "high"
        assert result["careers_page"] is True
        assert [f["preference_id"] for f in result["flags"]] == [pref.id]
        assert result["flags"][0]["type"] == "hard"
        assert "<<<UNTRUSTED COMPANY CONTENT>>>" in llm.calls[0]["prompt"]
        assert candidate.triaged_at is not None
    finally:
        db.close()


@pytest.mark.parametrize(
    "fetch",
    [
        lambda url: (_ for _ in ()).throw(httpx.ConnectError("down")),
        lambda url: ("<html></html>", ""),
    ],
)
def test_unreadable_site_is_marked_unknown_without_an_llm_call(monkeypatch, fetch):
    db = SessionLocal()
    try:
        candidate = _candidate(db)
        monkeypatch.setattr(triage, "_fetch_homepage", fetch)
        llm = FakeLLMProvider("{}")
        triage.triage_candidate(db, llm, candidate)
        assert candidate.triage["fit"] == "unknown" and candidate.triage["error"]
        assert llm.calls == []
    finally:
        db.close()


def test_unreadable_screening_answer_retries_once_then_is_unknown(monkeypatch):
    db = SessionLocal()
    try:
        candidate = _candidate(db)
        monkeypatch.setattr(triage, "_fetch_homepage", lambda url: ("<html></html>", LONG_TEXT))
        broken = '{"fit": "low", "flags": [{"preference_id": 1, "evidence_snippet": "x" (a comment)}]}'
        llm = SequencedLLMProvider([broken, "{}"])  # bad JSON, then no fit at all
        triage.triage_candidate(db, llm, candidate)
        assert len(llm.calls) == 2
        assert "not valid JSON" in llm.calls[1]["prompt"]
        assert candidate.triage["fit"] == "unknown"  # never a defaulted fit

        retried = _candidate(db)
        good = json.dumps({"summary": "Builds software.", "fit": "low", "flags": []})
        triage.triage_candidate(db, SequencedLLMProvider([broken, good]), retried)
        assert retried.triage["fit"] == "low"
    finally:
        db.close()


# --- approval ---


def test_approval_adds_a_tagged_company_under_its_brand_name_without_research():
    db = SessionLocal()
    try:
        candidate = _candidate(db, website="https://approve-me.example")
        candidate.triage = {"brand_name": "ApproveVision", "fit": "high"}
        candidate.contact_email = "jobs@approve-me.example"
        other = _candidate(db, website="https://reject-me.example")
        db.commit()

        assert service.approve(db, [candidate.id]) == 1
        assert service.reject(db, [other.id]) == 1

        company = db.get(Company, db.get(CompanyCandidate, candidate.id).company_id)
        assert company.name == "ApproveVision"
        assert (company.location, company.technopark) == ("Ankara", "Screen Park")
        assert db.get(CompanyCandidate, candidate.id).name in company.notes
        assert company.contact_email == "jobs@approve-me.example"
        assert not has_usable_research(company)  # lands in the dashboard's "needs research" queue
        assert db.get(CompanyCandidate, other.id).status == CandidateStatus.REJECTED
        assert service.approve(db, [candidate.id]) == 0  # approving twice adds nothing
    finally:
        db.close()


# --- routes ---


def test_discovery_page_lists_the_odtu_directory_by_default():
    page = client.get("/discovery")
    assert page.status_code == 200
    assert "ODTÜ Teknokent" in page.text


def test_decide_route_approves_selected_candidates():
    db = SessionLocal()
    try:
        candidate = _candidate(db, website="https://route-approve.example")
        candidate_id = candidate.id
    finally:
        db.close()

    r = client.post(
        "/discovery/decide",
        data={"candidate_ids": [str(candidate_id)], "action": "approve", "return_to": "https://evil.example"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/discovery"  # only redirects within the page
    db = SessionLocal()
    try:
        assert db.get(CompanyCandidate, candidate_id).status == CandidateStatus.APPROVED
    finally:
        db.close()


def test_screening_step_stops_and_shows_an_llm_failure(monkeypatch):
    class BrokenLLM(FakeLLMProvider):
        def generate(self, prompt, **kwargs):
            raise RuntimeError("no API credit")

    db = SessionLocal()
    try:
        candidate = _candidate(db, website="https://broken.example")
        source_id, candidate_id = candidate.source_id, candidate.id
    finally:
        db.close()
    monkeypatch.setattr(triage, "_fetch_homepage", lambda url: ("<html></html>", LONG_TEXT))
    app.dependency_overrides[get_llm] = lambda: BrokenLLM("{}")

    page = client.get(f"/discovery/triage/step?source_id={source_id}")

    assert "no API credit" in page.text
    assert 'http-equiv="refresh"' not in page.text  # stops instead of looping
    db = SessionLocal()
    try:
        assert db.get(CompanyCandidate, candidate_id).triaged_at is None  # retried next time
    finally:
        db.close()


def test_candidate_without_a_site_gets_a_google_search_and_can_be_given_one():
    db = SessionLocal()
    try:
        candidate = _candidate(db, website=None)
        candidate.name = "SİTESİZ YAZILIM A.Ş."
        candidate.triage = {"fit": "unknown", "error": "The directory lists no website."}
        db.commit()
        candidate_id, source_id = candidate.id, candidate.source_id
    finally:
        db.close()

    page = client.get(f"/discovery?source_id={source_id}&fit=unknown").text
    assert "https://www.google.com/search?q=S%C4%B0TES%C4%B0Z%20YAZILIM%20A.%C5%9E.%20Screen%20Park" in page
    assert f'action="/discovery/candidates/{candidate_id}/website"' in page

    r = client.post(
        f"/discovery/candidates/{candidate_id}/website",
        data={"website_url": "www.sitesiz.example", "return_to": f"/discovery?source_id={source_id}"},
        follow_redirects=False,
    )
    assert r.headers["location"] == f"/discovery?source_id={source_id}"
    db = SessionLocal()
    try:
        candidate = db.get(CompanyCandidate, candidate_id)
        assert candidate.website_url == "https://www.sitesiz.example"
        assert candidate.triaged_at is None and candidate.triage == {}  # back in the screening queue
    finally:
        db.close()


def test_row_buttons_decide_one_candidate_in_place():
    db = SessionLocal()
    try:
        keep = _candidate(db, website="https://row-keep.example")
        drop = _candidate(db, website="https://row-drop.example")
        keep_id, drop_id, source_id = keep.id, drop.id, keep.source_id
    finally:
        db.close()

    page = client.get(f"/discovery?source_id={source_id}").text
    assert 'class="row-decide"' in page and "✓" in page and "✕" in page
    assert 'name="candidate_ids" type="checkbox"' not in page and 'type="checkbox" name="candidate_ids"' not in page

    r = client.post(
        "/discovery/decide",
        data={"candidate_ids": str(drop_id), "action": "reject"},
        headers={"Accept": "application/json"},
    )
    assert r.json() == {"action": "reject", "changed": 1}
    db = SessionLocal()
    try:
        assert db.get(CompanyCandidate, drop_id).status == CandidateStatus.REJECTED
        assert db.get(CompanyCandidate, keep_id).status == CandidateStatus.PENDING
    finally:
        db.close()
