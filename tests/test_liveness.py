"""Roadmap phase 2: the no-LLM liveness check, keyword pre-screen and the new
discovery sources."""
from datetime import datetime, timezone

import httpx
from fastapi.testclient import TestClient

from app.db import SessionLocal, init_db
from app.discovery import liveness
from app.discovery.extra_sources import entries_from_rows, guess_mapping, read_json_endpoint, table_rows
from app.main import app
from app.models import CV, CandidateStatus, CompanyCandidate, DiscoverySource, Preference, PreferenceType
from app.preferences.keywords import keyword_flags

NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)
BODY = "<p>" + "We build industrial automation software for factories across Turkey. " * 8 + "</p>"


def _client(pages: dict[str, tuple[int, str] | str], headers: dict | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        page = pages.get(request.url.path)
        if page is None:
            return httpx.Response(404, text="")
        status, text = page if isinstance(page, tuple) else (200, page)
        return httpx.Response(status, text=text, headers=headers or {})

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def _check(pages, **kw):
    return liveness.check_liveness("https://acme.example/", client=_client(pages, kw.pop("headers", None)), resolver=lambda h: True, now=NOW, **kw)


def test_dead_host_is_dead_without_any_request():
    result = liveness.check_liveness("https://gone.example/", resolver=lambda h: False, now=NOW)
    assert result["status"] == "DEAD" and result["signals"][0]["name"] == "dns"


def test_http_error_and_parked_pages_are_dead():
    assert _check({"/": (503, "down")})["status"] == "DEAD"
    parked = _check({"/": "<html><body><h1>Bu alan adı satılıktır!</h1> İletişim için tıklayın.</body></html>"})
    assert parked["status"] == "DEAD"
    assert any(s["name"] == "parked" for s in parked["signals"])
    assert _check({"/": "<html><body><h1>Welcome to nginx!</h1></body></html>"})["status"] == "DEAD"


def test_old_sitemap_lastmod_makes_a_site_stale():
    sitemap = "<urlset><url><loc>https://acme.example/</loc><lastmod>2019-03-01</lastmod></url></urlset>"
    result = _check({"/": f"<html><body>{BODY}<footer>© 2019 Acme</footer></body></html>", "/sitemap.xml": sitemap})
    assert result["status"] == "STALE"
    names = {s["name"]: s["value"] for s in result["signals"]}
    assert names["sitemap_lastmod"] == "2019-03-01" and names["copyright_year"] == 2019


def test_recent_copyright_or_news_date_is_alive_and_no_signal_is_unknown():
    assert _check({"/": f"<html><body>{BODY}<footer>Copyright © 2015 - 2026 Acme</footer></body></html>"})["status"] == "ALIVE"
    news = _check({"/": f'<html><body>{BODY}<a href="/haberler">Haberler</a></body></html>', "/haberler": "<p>12 Ekim 2026 - Yeni ürün</p>"})
    assert news["status"] == "ALIVE"
    assert _check({"/": f"<html><body>{BODY}</body></html>"})["status"] == "UNKNOWN"


def test_js_redirect_shell_is_followed():
    shell = "<html><head><script>var l=['tr','en'];window.location.href='/'+l[0];</script></head><body></body></html>"
    result = _check({"/": shell, "/tr": f"<html><body>{BODY}<footer>© 2026</footer></body></html>"})
    assert result["status"] == "ALIVE"
    assert any(s["name"] == "js_redirect" for s in result["signals"])


def test_check_many_runs_one_request_at_a_time_per_host():
    active: dict[str, int] = {}
    peak: dict[str, int] = {}

    def fake_check(url):
        import time

        host = url.split("/")[2]
        active[host] = active.get(host, 0) + 1
        peak[host] = max(peak.get(host, 0), active[host])
        time.sleep(0.01)
        active[host] -= 1
        return {"status": "ALIVE", "signals": []}

    urls = [f"https://a.example/{i}" for i in range(4)] + [f"https://b{i}.example/" for i in range(6)]
    results = liveness.check_many(urls, concurrency=8, check=fake_check)
    assert len(results) == 10 and peak["a.example"] == 1


def test_keyword_pre_screen_flags_with_snippet_and_turkish_suffixes():
    pref = Preference(id=1, description="No defense industry", type=PreferenceType.HARD, signals=["savunma", "defence"])
    flags = keyword_flags("Şirketimiz Savunmaya yönelik elektronik sistemler üretir.", [pref], "https://x.example")
    assert flags[0]["keyword"] == "savunma" and "Savunmaya" in flags[0]["evidence_snippet"]
    assert keyword_flags("We build accounting software.", [pref]) == []


def test_csv_import_with_guessed_mapping_and_json_endpoint_reader(monkeypatch):
    rows = table_rows("arge.csv", "Firma Unvanı;Web Sitesi;İl\nACME YAZILIM A.Ş.;acme.example;Ankara\n;;\n".encode())
    mapping = guess_mapping(list(rows[0]))
    assert mapping == {"name": "Firma Unvanı", "website": "Web Sitesi", "city": "İl"}
    (entry,) = entries_from_rows(rows, mapping)
    assert entry["name"] == "ACME YAZILIM A.Ş." and entry["website_url"].startswith("https://acme.example") and entry["city"] == "Ankara"

    import app.discovery.extra_sources as extra_sources

    data = {"data": {"items": [{"firm": {"title": "Gazi Soft"}, "web": "https://gazisoft.example"}, {"firm": {"title": ""}}]}}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=data))
    monkeypatch.setattr(extra_sources, "_client", lambda timeout=30: httpx.Client(transport=transport))
    (json_entry,) = read_json_endpoint("https://gazi.example/api", {"items_path": "data.items", "name": "firm.title", "website": "web"})
    assert json_entry["name"] == "Gazi Soft"


def test_discover_page_filters_by_liveness_and_screening_skips_dead():
    from app.web.routes.discovery import _screening_queue

    init_db()
    db = SessionLocal()
    try:
        if db.query(CV).first() is None:
            db.add(CV(name="Liveness Gate CV", file_path=""))
        source = DiscoverySource(name="Live Test Park", url="https://park.example/live", kind="html")
        db.add(source)
        db.commit()
        db.add_all([
            CompanyCandidate(source_id=source.id, name="Dead Firm", website_url="https://dead.example", status=CandidateStatus.PENDING, liveness={"status": "DEAD", "signals": []}),
            CompanyCandidate(source_id=source.id, name="Alive Firm", website_url="https://alive.example", status=CandidateStatus.PENDING, liveness={"status": "ALIVE", "signals": []}),
        ])
        db.commit()
        source_id = source.id
        assert [c.name for c in _screening_queue(db, source_id, include_dead=False)] == ["Alive Firm"]
        assert len(_screening_queue(db, source_id, include_dead=True)) == 2
    finally:
        db.close()
    page = TestClient(app).get(f"/discovery?source_id={source_id}&live=DEAD")
    assert page.status_code == 200 and "Dead Firm" in page.text and "Alive Firm" not in page.text
