"""LinkedIn research orchestration: fetch the company's LinkedIn pages, turn the
raw page text into structured findings with one LLM call, cache them on the company.

Answers the questions the owner otherwise checks by hand before applying
(CLAUDE.md section 5 step 1):
- how big is the company on LinkedIn, and what do its people actually do
- who is worth contacting there (surfaced as suggested contacts, never emailed
  automatically - LinkedIn gives names and roles, never addresses)
- is the company still alive (recent company posts)
- what is it hiring for on LinkedIn, next to what its own careers page says

The MCP tools return raw page text, so the structuring happens here. Everything
kept is evidence-first: people are dropped unless their name appears in the
scraped employee list, and each person keeps their own LinkedIn profile URL.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.companies.linkedin_fetch import fetch_linkedin_company
from app.config import get_settings
from app.llm.base import LLMProvider
from app.llm.json_utils import parse_json_object
from app.models import Company

LINKEDIN_SYSTEM_PROMPT = """You are reading the raw text of a company's LinkedIn pages \
(about page, recent posts, job listings, the employees page, and several keyword searches \
of that employees page) on behalf of a job applicant. Base EVERYTHING only on the given \
text - never invent people, headcounts, dates or openings, and never fall back on what you \
may know about the company. Where the text does not say, use null or an empty list.

Report, as JSON:
1. "employees_listed": how many people LinkedIn actually lists on the employees page, \
exactly as written there (e.g. "273 çalışan", "273 employees", "8 associated members"), \
else null. This is NOT the about page's company-size bracket ("1.001-5.000 çalışan", \
"1001-5000 employees") - that is the company's own declaration of its size and must be \
ignored completely. If only the bracket is present, return null.
2. "industry": the industry line as written, else "". "headquarters": the location line as \
written, else "". "founded": year as written, else "".
3. "positions_summary": what the company's people actually do. Prefer the employees page's \
own function breakdown (e.g. "Engineering 42", "Sales 12") when the text has one; otherwise \
group the listed employees' job titles yourself. 3-8 entries, each {"group": string, \
"count": integer or null (null when you grouped titles yourself and cannot count reliably), \
"source": "linkedin_breakdown" if it came from LinkedIn's own breakdown, else "titles"}. \
Empty list if the text lists no people.
4. "activity": {"last_post_date": the most recent post's date as written (e.g. "2w", \
"3 months ago", "2026-05-01") or null if no posts are shown, "is_active": true if that most \
recent post is within roughly 3 months of TODAY, false if clearly older, null if unknown, \
"assessment": 1-2 sentences on whether the company looks alive on LinkedIn, based only on \
the posts text}.
5. "jobs": openings literally listed in the text, each {"title": as written, "location": as \
written or ""}. Empty list if none are listed.
6. "key_people": up to 10 people from the text who could actually be contacted about a job \
application - founders/owners, C-level (CEO, CTO, ...), directors and managers, team and \
engineering leads, HR / people / recruiting staff. Each {"name": copied EXACTLY as it \
appears, "role": their title exactly as written}. Three hard rules:
   - ONLY decision-makers and hiring people. An individual contributor (developer, \
support specialist, analyst, intern) does NOT belong here no matter how few people the \
text lists.
   - DROP anyone whose role text names a DIFFERENT employer than this company. The keyword \
searches match a title anywhere in a person's headline, so someone who is "CEO at Other Co" \
while merely following this company will appear - they are not a contact here.
   - If nobody qualifies, return an empty list. Never pad it.

Output ONLY valid JSON, this exact shape:
{"employees_listed": string | null, "industry": string, "headquarters": string, \
"founded": string, "positions_summary": [{"group": string, "count": integer | null, \
"source": string}], "activity": {"last_post_date": string | null, "is_active": boolean | null, \
"assessment": string}, "jobs": [{"title": string, "location": string}], \
"key_people": [{"name": string, "role": string}]}
"""

LINKEDIN_FALLBACK = {
    "employees_listed": None,
    "industry": "",
    "headquarters": "",
    "founded": "",
    "positions_summary": [],
    "activity": {},
    "jobs": [],
    "key_people": [],
}

MAX_POSITION_GROUPS = 8
MAX_KEY_PEOPLE = 10
MAX_JOBS = 20
# The sections are individually capped in linkedin_fetch (6000 for the main
# pages, 3000 per keyword search); this is the backstop for their sum.
MAX_PROMPT_CHARS = 40000


def _normalize_name(name: str) -> str:
    return " ".join(name.split()).casefold()


def _build_prompt(fetched: dict) -> str:
    blocks = [f"[{name}]\n{text}" for name, text in fetched["sections"].items()]
    if fetched["people"]:
        listed = "\n".join(f"- {p['name']}" for p in fetched["people"] if p["name"])
        if listed:
            blocks.append(f"[employee profile links found on the page]\n{listed}")
    today = datetime.now(timezone.utc).date().isoformat()
    return f"TODAY: {today}\n\n" + "\n\n---\n\n".join(blocks)[:MAX_PROMPT_CHARS]


def _key_people(data: dict, fetched: dict) -> list[dict]:
    """Model-picked people, kept only when the scrape actually listed them.

    The model chooses who is worth contacting, but it may not introduce anyone:
    a name with no match in the scraped profile links is dropped, and the URL
    stored is the one that came off the page (Evidence Rule).
    """
    by_name = {_normalize_name(p["name"]): p for p in fetched["people"] if p.get("name")}
    people = []
    seen: set[str] = set()
    for entry in data.get("key_people", []) or []:
        name = (entry.get("name") or "").strip()
        match = by_name.get(_normalize_name(name))
        if not name or match is None or name in seen:
            continue
        seen.add(name)
        people.append(
            {
                "name": name,
                "role": (entry.get("role") or "").strip(),
                "profile_url": match["profile_url"],
            }
        )
        if len(people) >= MAX_KEY_PEOPLE:
            break
    return people


def linkedin_research_company(db: Session, llm: LLMProvider, company: Company) -> Company:
    if not company.linkedin_url:
        raise ValueError(
            "Company has no LinkedIn URL set - none was found on the website, so add it by hand."
        )

    settings = get_settings()
    fetched = fetch_linkedin_company(
        company.linkedin_url,
        command=settings.linkedin_mcp_command,
        timeout_seconds=settings.linkedin_mcp_timeout_seconds,
        tool_delay_seconds=settings.linkedin_tool_delay_seconds,
        people_keywords=settings.linkedin_people_keywords,
    )

    response = llm.generate(
        prompt=_build_prompt(fetched),
        system=LINKEDIN_SYSTEM_PROMPT,
        temperature=0.0,
        max_output_tokens=1200,
    )
    data = parse_json_object(response.text, fallback=LINKEDIN_FALLBACK)

    positions = []
    for entry in (data.get("positions_summary") or [])[:MAX_POSITION_GROUPS]:
        group = (entry.get("group") or "").strip()
        if not group:
            continue
        count = entry.get("count")
        positions.append(
            {
                "group": group,
                "count": count if isinstance(count, int) else None,
                "source": (entry.get("source") or "").strip(),
            }
        )

    jobs = []
    for entry in (data.get("jobs") or [])[:MAX_JOBS]:
        title = (entry.get("title") or "").strip()
        if title:
            jobs.append({"title": title, "location": (entry.get("location") or "").strip()})

    activity = data.get("activity") or {}
    company.linkedin_research = {
        "source_url": fetched["source_url"],
        # People LinkedIn actually lists, not the company's own size bracket.
        "employees_listed": (data.get("employees_listed") or "").strip() or None,
        "industry": (data.get("industry") or "").strip(),
        "headquarters": (data.get("headquarters") or "").strip(),
        "founded": (data.get("founded") or "").strip(),
        "positions_summary": positions,
        "activity": {
            "last_post_date": (activity.get("last_post_date") or "").strip() or None,
            "is_active": activity.get("is_active") if isinstance(activity.get("is_active"), bool) else None,
            "assessment": (activity.get("assessment") or "").strip(),
        },
        "jobs": jobs,
        "people": _key_people(data, fetched),
        # Which of the two tool calls failed, if any - a half-empty card should
        # say why rather than look like a company with nothing on LinkedIn.
        "tool_errors": fetched["tool_errors"],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    db.commit()
    db.refresh(company)
    return company


def linkedin_research_company_safe(db: Session, llm: LLMProvider, company: Company) -> Company:
    """Same as linkedin_research_company, but records a failure (server not
    installed, expired session, timeout, ...) on the company instead of raising,
    so it shows up as a visible error on the company page. Never touches
    company.research - website research and LinkedIn research fail apart."""
    try:
        return linkedin_research_company(db, llm, company)
    except Exception as exc:  # noqa: BLE001 - surfaced on the company page, not swallowed
        company.linkedin_research = {
            "error": str(exc),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        db.commit()
        db.refresh(company)
        return company
