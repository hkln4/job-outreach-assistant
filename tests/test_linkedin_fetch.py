"""LinkedIn retrieval helpers: slug parsing and the shaping of whatever the MCP
server hands back. The MCP subprocess itself is never spawned here - these cover
the pure functions around it."""
import json

import pytest

import app.companies.linkedin_fetch as lf


class FakeBlock:
    def __init__(self, text: str):
        self.text = text


class FakeResult:
    def __init__(self, *, structured=None, text="", is_error=False):
        self.structuredContent = structured
        self.content = [FakeBlock(text)] if text else []
        self.isError = is_error


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.linkedin.com/company/acme", "acme"),
        ("https://www.linkedin.com/company/acme/", "acme"),
        ("https://linkedin.com/company/acme-corp/about/", "acme-corp"),
        ("https://tr.linkedin.com/company/acme?trk=footer", "acme"),
        ("www.linkedin.com/company/acme", "acme"),
        ("https://www.linkedin.com/in/some-person", None),
        ("https://www.linkedin.com/jobs/view/123", None),
        ("https://acme.example/company/acme", None),
        ("", None),
    ],
)
def test_linkedin_company_slug(url, expected):
    assert lf.linkedin_company_slug(url) == expected


def test_tool_payload_prefers_structured_content():
    payload = {"url": "https://www.linkedin.com/company/acme", "sections": {"about": "About Acme"}}
    assert lf._tool_payload(FakeResult(structured=payload)) == payload


def test_tool_payload_falls_back_to_json_text():
    payload = {"sections": {"about": "About Acme"}}
    assert lf._tool_payload(FakeResult(text=json.dumps(payload))) == payload


def test_tool_payload_keeps_prose_answers_as_a_section():
    assert lf._tool_payload(FakeResult(text="Acme has 42 employees")) == {
        "sections": {"raw": "Acme has 42 employees"}
    }


def test_tool_payload_raises_on_error_result():
    with pytest.raises(lf.LinkedInFetchError, match="session expired"):
        lf._tool_payload(FakeResult(text="session expired", is_error=True))


def test_sections_are_trimmed_and_blank_ones_dropped():
    sections = lf._sections({"sections": {"about": "x" * 9999, "posts": "   ", "jobs": None}})
    assert list(sections) == ["about"]
    assert len(sections["about"]) == lf.MAX_CHARS_PER_SECTION


def test_people_refs_keep_person_links_only_and_dedupe():
    payload = {
        "references": {
            "employees": [
                {"kind": "person", "url": "https://www.linkedin.com/in/ada", "text": "Ada Lovelace"},
                {"kind": "person", "url": "https://www.linkedin.com/in/ada", "text": "Ada Lovelace"},
                {"kind": "company", "url": "https://www.linkedin.com/company/acme", "text": "Acme"},
                {"kind": "person", "url": "https://www.linkedin.com/in/grace", "text": "Grace Hopper"},
            ]
        }
    }
    assert lf._people_refs(payload) == [
        {"name": "Ada Lovelace", "profile_url": "https://www.linkedin.com/in/ada"},
        {"name": "Grace Hopper", "profile_url": "https://www.linkedin.com/in/grace"},
    ]


def test_fetch_rejects_non_company_urls_without_spawning_anything():
    with pytest.raises(lf.LinkedInFetchError, match="Not a LinkedIn company URL"):
        lf.fetch_linkedin_company(
            "https://www.linkedin.com/in/someone",
            command="uvx mcp-server-linkedin@latest",
            timeout_seconds=5,
            tool_delay_seconds=0,
        )


def test_fetch_reports_a_missing_server_instead_of_crashing():
    with pytest.raises(lf.LinkedInFetchError, match="Could not run the LinkedIn MCP server"):
        lf.fetch_linkedin_company(
            "https://www.linkedin.com/company/acme",
            command="definitely-not-an-installed-binary-9x8y7z",
            timeout_seconds=5,
            tool_delay_seconds=0,
        )


def test_profile_urls_are_absolutised():
    """The server canonicalizes LinkedIn-internal links down to bare paths;
    left as-is they resolve against our own host, not linkedin.com."""
    payload = {
        "references": {
            "employees": [
                {"kind": "person", "url": "/in/elif-akduman-193a44191/", "text": "Elif AKDUMAN"},
                {"kind": "person", "url": "https://www.linkedin.com/in/ada", "text": "Ada"},
            ]
        }
    }
    assert lf._people_refs(payload) == [
        {"name": "Elif AKDUMAN", "profile_url": "https://www.linkedin.com/in/elif-akduman-193a44191/"},
        {"name": "Ada", "profile_url": "https://www.linkedin.com/in/ada"},
    ]


def test_keyword_sections_are_labelled_so_they_do_not_overwrite_each_other():
    # Every keyword search comes back under the same section name.
    payload = {"sections": {"employees": "Ada Lovelace - CTO"}}
    plain = lf._sections(payload)
    ceo = lf._sections(payload, label="CEO", max_chars=lf.MAX_CHARS_PER_KEYWORD_SECTION)
    cto = lf._sections(payload, label="CTO", max_chars=lf.MAX_CHARS_PER_KEYWORD_SECTION)
    merged = {**plain, **ceo, **cto}
    assert len(merged) == 3
    assert "employees (search: CEO)" in merged


def test_keyword_sections_get_the_smaller_cap():
    payload = {"sections": {"employees": "x" * 9999}}
    section = lf._sections(payload, label="CEO", max_chars=lf.MAX_CHARS_PER_KEYWORD_SECTION)
    assert len(section["employees (search: CEO)"]) == lf.MAX_CHARS_PER_KEYWORD_SECTION


def test_parse_people_keywords():
    assert lf.parse_people_keywords("CEO, CTO ,müdür") == ["CEO", "CTO", "müdür"]
    assert lf.parse_people_keywords("CEO,ceo, CEO") == ["CEO"]  # deduped, first spelling wins
    assert lf.parse_people_keywords("  ") == []
    assert lf.parse_people_keywords("") == []
