"""LinkedIn company data retrieval (CLAUDE.md section 9, LinkedIn research rule).

Mirrors companies/fetch.py's role for LinkedIn: pure retrieval, no DB and no LLM,
so it is the single seam the tests fake. Retrieval stays in the app - the model
never drives the browser, it only reads the text this module brings back.

The backend runs stickerdaniel/linkedin-mcp-server as a short-lived stdio
subprocess (`uvx mcp-server-linkedin@latest` - note the PyPI name reverses the
words, and "linkedin-mcp-server" there is an unrelated project), which drives a
real browser using a session the owner logged in once with a DEDICATED
THROWAWAY ACCOUNT. LinkedIn's
terms forbid automated access, so this whole module is only reachable when
LINKEDIN_RESEARCH_ENABLED is on, is triggered per company by hand, and never
touches the messaging or connection-request tools.

Calls per company:
  get_company_profile(slug, sections="posts,jobs") -> about + posts + jobs
  get_company_employees(slug)                      -> people list + demographics
  get_company_employees(slug, keywords=<term>)     -> one per configured term
The keyword calls are what surface decision-makers: the unfiltered /people/ page
returns whatever LinkedIn ranks first, which at a mid-size company is rank-and-
file. Searching "CEO", "müdür", "insan kaynakları" etc. is the same thing as
typing them into the people tab's search box by hand.

The tools return raw page text, not structured records; parsing it into
headcount/roles/people is the LLM's job in linkedin_workflow.py.
"""
import asyncio
import json
import shlex
from datetime import timedelta
from typing import Any
from urllib.parse import urljoin, urlparse

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

TOOL_PROFILE = "get_company_profile"
TOOL_EMPLOYEES = "get_company_employees"
# The about page is always included; these are the extras worth the page loads.
PROFILE_SECTIONS = "posts,jobs"

# Each section is one LinkedIn page's innerText. Head-only truncation (unlike
# the website fetch, which keeps a tail for footer emails) - a LinkedIn page's
# tail is site chrome, and everything useful sits at the top.
MAX_CHARS_PER_SECTION = 6000
# Keyword searches get a smaller share: there are several of them and each
# returns a short result list, so they must not crowd out the main sections.
MAX_CHARS_PER_KEYWORD_SECTION = 3000
# Profile links of listed employees, used as per-person evidence URLs.
MAX_PEOPLE_REFS = 120

# LinkedIn-internal links come back as paths ("/in/some-person/") because the
# server canonicalizes them; they are only usable once absolutised.
LINKEDIN_BASE = "https://www.linkedin.com"

# Per tool call, a little above the server's own 180s per-tool timeout so its
# specific error surfaces instead of ours. The whole run is bounded separately.
PER_CALL_TIMEOUT = timedelta(seconds=200)


class LinkedInFetchError(RuntimeError):
    """The MCP server could not be reached, started, or authenticated."""


def linkedin_company_slug(linkedin_url: str) -> str | None:
    """"https://www.linkedin.com/company/acme/about/?x=1" -> "acme".

    The MCP tools address companies by URL slug, not display name, so this is
    the only identifier they accept. Returns None for anything that is not a
    LinkedIn company URL.
    """
    if not linkedin_url:
        return None
    candidate = linkedin_url.strip()
    if not candidate.startswith(("http://", "https://")):
        candidate = "https://" + candidate
    parsed = urlparse(candidate)
    host = parsed.netloc.lower()
    if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2 or parts[0].lower() != "company":
        return None
    return parts[1] or None


def _tool_payload(result: Any) -> dict:
    """The tool's returned dict, from either structured or text content."""
    if getattr(result, "isError", False):
        raise LinkedInFetchError(_result_text(result) or "tool call failed")
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    text = _result_text(result)
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        # Older/plain servers may answer with prose instead of a JSON dict;
        # keep it rather than dropping the whole call on the floor.
        return {"sections": {"raw": text}} if text else {}
    return parsed if isinstance(parsed, dict) else {"sections": {"raw": text}}


def _result_text(result: Any) -> str:
    parts = [getattr(block, "text", "") for block in getattr(result, "content", []) or []]
    return "\n".join(p for p in parts if p).strip()


def _sections(payload: dict, *, label: str = "", max_chars: int = MAX_CHARS_PER_SECTION) -> dict[str, str]:
    """Named page texts. `label` distinguishes the keyword searches, which all
    come back under the same section name and would otherwise overwrite each
    other - and it tells the model which search produced which list."""
    sections = payload.get("sections")
    if not isinstance(sections, dict):
        return {}
    out = {}
    for name, text in sections.items():
        if isinstance(text, str) and text.strip():
            key = f"{name} (search: {label})" if label else str(name)
            out[key] = text.strip()[:max_chars]
    return out


def _people_refs(payload: dict) -> list[dict]:
    """Employee profile links from the tool's `references`, deduped by URL.

    The server canonicalizes LinkedIn-internal links down to bare paths, so
    these have to be absolutised or they resolve against our own host.
    """
    references = payload.get("references")
    if not isinstance(references, dict):
        return []
    people: list[dict] = []
    seen: set[str] = set()
    for entries in references.values():
        for ref in entries or []:
            if not isinstance(ref, dict) or ref.get("kind") != "person":
                continue
            url = (ref.get("url") or "").strip()
            name = (ref.get("text") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            people.append({"name": name, "profile_url": urljoin(LINKEDIN_BASE, url)})
            if len(people) >= MAX_PEOPLE_REFS:
                return people
    return people


async def _run(
    slug: str,
    command_args: list[str],
    tool_delay_seconds: float,
    people_keywords: list[str],
) -> dict:
    # UV_HTTP_TIMEOUT: the first run downloads the server and its browser
    # dependencies through uvx, which overruns uv's default HTTP timeout.
    params = StdioServerParameters(
        command=command_args[0],
        args=command_args[1:],
        env={**get_default_environment(), "UV_HTTP_TIMEOUT": "300"},
    )
    sections: dict[str, str] = {}
    people: list[dict] = []
    tool_errors: dict[str, str] = {}
    source_url = f"https://www.linkedin.com/company/{slug}"

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # (tool, arguments, section label, section cap). The unfiltered
            # employees call stays first so its demographics drive the role
            # breakdown; the keyword searches then dig out decision-makers.
            calls = [
                (TOOL_PROFILE, {"company_name": slug, "sections": PROFILE_SECTIONS}, "", MAX_CHARS_PER_SECTION),
                (TOOL_EMPLOYEES, {"company_name": slug}, "", MAX_CHARS_PER_SECTION),
            ]
            calls += [
                (
                    TOOL_EMPLOYEES,
                    {"company_name": slug, "keywords": keyword},
                    keyword,
                    MAX_CHARS_PER_KEYWORD_SECTION,
                )
                for keyword in people_keywords
            ]

            for index, (tool_name, arguments, label, max_chars) in enumerate(calls):
                if index:
                    # Space out page loads - rate hygiene even on a throwaway.
                    await asyncio.sleep(tool_delay_seconds)
                # A fixed per-call ceiling rather than a share of the whole
                # budget: the server's own per-tool timeout is 180s, and a
                # share would shrink below that as keywords are added, so our
                # blunter error would pre-empt its precise one.
                try:
                    payload = _tool_payload(
                        await session.call_tool(
                            tool_name, arguments, read_timeout_seconds=PER_CALL_TIMEOUT
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - one failed call must not lose the others' data
                    tool_errors[f"{tool_name}({label})" if label else tool_name] = str(exc)
                    continue
                sections.update(_sections(payload, label=label, max_chars=max_chars))
                people.extend(p for p in _people_refs(payload) if p not in people)
                if tool_name == TOOL_PROFILE and payload.get("url"):
                    source_url = payload["url"]

    if not sections and tool_errors:
        # Both tools failed: that's a fetch failure, not a thin company page.
        raise LinkedInFetchError("; ".join(f"{k}: {v}" for k, v in tool_errors.items()))
    return {
        "slug": slug,
        "source_url": source_url,
        "sections": sections,
        "people": people[:MAX_PEOPLE_REFS],
        "tool_errors": tool_errors,
    }


def parse_people_keywords(raw: str) -> list[str]:
    """"CEO, CTO, müdür" -> ["CEO", "CTO", "müdür"] (deduped, order kept).
    Each term costs one more page load, so an empty setting is honoured as
    "skip the targeted search entirely"."""
    seen: set[str] = set()
    keywords = []
    for term in (raw or "").split(","):
        term = term.strip()
        if term and term.casefold() not in seen:
            seen.add(term.casefold())
            keywords.append(term)
    return keywords


def fetch_linkedin_company(
    linkedin_url: str,
    *,
    command: str,
    timeout_seconds: int,
    tool_delay_seconds: float,
    people_keywords: str = "",
) -> dict:
    """Returns {"slug", "source_url", "sections": {name: text}, "people":
    [{"name", "profile_url"}], "tool_errors": {call: message}}.

    Raises LinkedInFetchError when the server cannot be used at all (not
    installed, session expired, company page unreachable); a single failing
    call is recorded in tool_errors instead, so a partial run still yields data.
    Called from a sync request handler, hence asyncio.run - FastAPI runs sync
    endpoints in a worker thread, so there is no running loop to clash with.
    """
    slug = linkedin_company_slug(linkedin_url)
    if not slug:
        raise LinkedInFetchError(f"Not a LinkedIn company URL: {linkedin_url!r}")
    command_args = shlex.split(command)
    if not command_args:
        raise LinkedInFetchError("LINKEDIN_MCP_COMMAND is empty.")
    keywords = parse_people_keywords(people_keywords)

    async def _with_timeout() -> dict:
        return await asyncio.wait_for(
            _run(slug, command_args, tool_delay_seconds, keywords), timeout_seconds
        )

    try:
        return asyncio.run(_with_timeout())
    except LinkedInFetchError:
        raise
    except asyncio.TimeoutError as exc:
        raise LinkedInFetchError(f"LinkedIn research timed out after {timeout_seconds}s.") from exc
    except Exception as exc:  # noqa: BLE001 - surfaced on the company page by the _safe wrapper
        raise LinkedInFetchError(f"Could not run the LinkedIn MCP server ({command}): {exc}") from exc
