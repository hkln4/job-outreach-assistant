"""Careers-page deep fetch: job-posting link discovery and the thin-page
(JS-rendered) flag."""
import httpx

import app.companies.fetch as fetch

CAREERS_HTML = """
<html><body>
  <a href="/careers">Careers</a>
  <a href="/careers/backend-engineer">Backend Engineer</a>
  <a href="/careers/ml-uzman-ilani">ML Uzmanı</a>
  <a href="https://www.kariyer.net/acme">Apply on kariyer.net</a>
  <a href="mailto:hr@acme.example">hr@acme.example</a>
  <a href="/files/brochure.pdf">Job brochure (PDF)</a>
  <a href="/about">About us</a>
</body></html>
"""


def test_find_job_links_same_site_postings_only():
    links = fetch._find_job_links(CAREERS_HTML, "https://acme.example/careers", "https://acme.example")
    assert links == [
        "https://acme.example/careers/backend-engineer",
        "https://acme.example/careers/ml-uzman-ilani",
    ]


def _mock_site(monkeypatch, pages: dict[str, str]):
    def handler(request: httpx.Request) -> httpx.Response:
        html = pages.get(request.url.path)
        if html is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text=html)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(fetch.httpx, "Client", lambda **kw: real_client(transport=transport, **kw))


def test_fetch_follows_job_posting_pages(monkeypatch):
    filler = "We build widget analytics platforms for logistics companies. " * 10
    _mock_site(
        monkeypatch,
        {
            "/": f'<html><body><a href="/careers">Careers</a><p>{filler}</p></body></html>',
            "/careers": f'<html><body><p>{filler}</p><a href="/careers/backend-engineer">Backend Engineer</a></body></html>',
            "/careers/backend-engineer": f"<html><body><p>Backend Engineer: Python, FastAPI, Postgres. {filler}</p></body></html>",
        },
    )

    result = fetch.fetch_company_research("https://acme.example/")
    urls = [p["url"] for p in result["pages"]]
    assert "https://acme.example/careers/backend-engineer" in urls
    assert result["careers_page_thin"] is False
    assert any("Python, FastAPI, Postgres" in p["text"] for p in result["pages"])


def test_pseudo_links_never_crash_research(monkeypatch):
    # A "Contact" menu item with href="javascript:;" used to win the weak-keyword
    # match, producing an unfetchable URL ("unknown url type: '/;'").
    filler = "We build widget analytics platforms for logistics companies. " * 10
    _mock_site(
        monkeypatch,
        {
            "/": (
                '<html><body><a href="javascript:;">Contact</a><a href="javascript:;">Company</a>'
                f'<a href="/real-contact">Contact us</a><p>{filler}</p></body></html>'
            ),
            "/real-contact": f"<html><body><p>Reach us at hr@acme.example. {filler}</p></body></html>",
        },
    )

    result = fetch.fetch_company_research("https://acme.example/")
    urls = [p["url"] for p in result["pages"]]
    # the pseudo-link is skipped AND the real contact page still gets found
    assert "https://acme.example/real-contact" in urls


def test_fetch_flags_thin_js_rendered_careers_page(monkeypatch):
    filler = "We build widget analytics platforms for logistics companies. " * 10
    _mock_site(
        monkeypatch,
        {
            "/": f'<html><body><a href="/careers">Careers</a><p>{filler}</p></body></html>',
            "/careers": '<html><body><div id="app"></div></body></html>',  # JS shell
        },
    )

    result = fetch.fetch_company_research("https://acme.example/")
    assert result["careers_page_thin"] is True


# --- LinkedIn company link discovery (feeds the LinkedIn research action) ---


def test_linkedin_link_normalized_from_a_footer_icon():
    html = """
    <html><body>
      <a href="https://www.linkedin.com/in/ceo-person">Our CEO</a>
      <a href="https://tr.linkedin.com/company/acme-yazilim/about/?trk=footer">LinkedIn</a>
    </body></html>
    """
    assert (
        fetch._find_linkedin_link(html, "https://acme.example")
        == "https://www.linkedin.com/company/acme-yazilim"
    )


def test_personal_profiles_and_share_widgets_are_not_company_pages():
    html = """
    <html><body>
      <a href="https://www.linkedin.com/in/some-person">Founder</a>
      <a href="https://www.linkedin.com/sharing/share-offsite/?url=https://acme.example">Share</a>
      <a href="https://twitter.com/acme">Twitter</a>
    </body></html>
    """
    assert fetch._find_linkedin_link(html, "https://acme.example") is None


def test_fetch_detects_the_linkedin_page_from_the_homepage(monkeypatch):
    filler = "We build widget analytics platforms for logistics companies. " * 10
    _mock_site(
        monkeypatch,
        {
            "/": (
                '<html><body><a href="https://www.linkedin.com/company/acme/">LinkedIn</a>'
                f"<p>{filler}</p></body></html>"
            ),
        },
    )

    result = fetch.fetch_company_research("https://acme.example/")
    assert result["detected_linkedin_url"] == "https://www.linkedin.com/company/acme"


def test_linkedin_link_is_also_found_on_a_sub_page(monkeypatch):
    """Plenty of sites only put the social icons in the contact page's footer."""
    filler = "We build widget analytics platforms for logistics companies. " * 10
    _mock_site(
        monkeypatch,
        {
            "/": f'<html><body><a href="/contact">Contact us</a><p>{filler}</p></body></html>',
            "/contact": (
                '<html><body><a href="https://www.linkedin.com/company/acme/">LinkedIn</a>'
                f"<p>{filler}</p></body></html>"
            ),
        },
    )

    result = fetch.fetch_company_research("https://acme.example/")
    assert result["detected_linkedin_url"] == "https://www.linkedin.com/company/acme"


def test_find_linkedin_url_reads_the_homepage_only_when_it_can(monkeypatch):
    """The cheap lookup for companies researched before LinkedIn research
    existed: no text extraction, no LLM, and no second page when the homepage
    already links LinkedIn."""
    fetched = []

    def handler(request):
        fetched.append(request.url.path)
        pages = {
            "/": '<html><body><a href="/contact">Contact us</a>'
                 '<a href="https://tr.linkedin.com/company/acme/?trk=footer">LinkedIn</a></body></html>',
            "/contact": "<html><body>should not be needed</body></html>",
        }
        html = pages.get(request.url.path)
        return httpx.Response(200, text=html) if html else httpx.Response(404, text="")

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(fetch.httpx, "Client", lambda **kw: real_client(transport=transport, **kw))

    assert fetch.find_linkedin_url("https://acme.example/") == "https://www.linkedin.com/company/acme"
    assert fetched == ["/"]


def test_find_linkedin_url_falls_back_to_the_contact_page(monkeypatch):
    def handler(request):
        pages = {
            "/": '<html><body><a href="/contact">Contact us</a></body></html>',
            "/contact": '<html><body><a href="https://www.linkedin.com/company/acme/">LinkedIn</a></body></html>',
        }
        html = pages.get(request.url.path)
        return httpx.Response(200, text=html) if html else httpx.Response(404, text="")

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(fetch.httpx, "Client", lambda **kw: real_client(transport=transport, **kw))

    assert fetch.find_linkedin_url("https://acme.example/") == "https://www.linkedin.com/company/acme"


def test_find_linkedin_url_returns_none_when_the_site_has_no_link(monkeypatch):
    _mock_site(monkeypatch, {"/": "<html><body><p>No socials here.</p></body></html>"})
    assert fetch.find_linkedin_url("https://acme.example/") is None
    assert fetch.find_linkedin_url("") is None


def test_js_language_splash_is_followed(monkeypatch):
    """Some sites serve a 161-byte shell whose only job is a JS redirect to
    /tr or /en; a plain fetch stops there and extracts nothing."""
    body = "We build airport inspection systems for civil aviation customers. " * 12
    _mock_site(
        monkeypatch,
        {
            "/": "<html><body><script>var a=['en','tr'];"
                 "window.location.href='/'+(navigator.languages.find(l=>a.includes(l))||a[0]);</script></body></html>",
            "/en": f"<html><body><p>{body}</p></body></html>",
        },
    )

    result = fetch.fetch_company_research("https://splash.example/")
    assert result["pages"][0]["url"] == "https://splash.example/en"
    assert result["content_chars"] > fetch.MIN_USABLE_RESEARCH_CHARS


def test_empty_page_reports_zero_content(monkeypatch):
    _mock_site(monkeypatch, {"/": "<html><body></body></html>"})
    result = fetch.fetch_company_research("https://empty.example/")
    assert result["content_chars"] == 0


def test_js_redirect_candidates_only_fire_on_redirect_scripts():
    plain = '<html><body><script>console.log("tr")</script></body></html>'
    assert fetch._js_redirect_candidates(plain, "https://x.example") == []

    redirecting = "<html><body><script>window.location.href='/tr/';</script></body></html>"
    assert fetch._js_redirect_candidates(redirecting, "https://x.example") == ["https://x.example/tr/"]
