"""
Central app configuration, loaded from .env (see .env.example).
Nothing else in the app should read os.environ directly - go through Settings.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM provider (section 8)
    llm_provider: str = "gemini"  # "gemini" | "claude"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5"  # cheapest tier - reading/extraction tasks don't need more
    # Letters a recruiter actually reads (drafts, follow-ups, escalations) get a
    # stronger writer than the bulk reading tasks: Haiku's Turkish read stiff
    # and it kept using phrases the prompt bans. One call per letter, so the
    # cost difference stays small. Empty = use the provider's main model.
    anthropic_draft_model: str = "claude-sonnet-5-5"
    gemini_draft_model: str = ""

    # Storage
    database_url: str = "sqlite:///data/app.db"

    # GitHub ingestion (token identity is used directly - GET /user - so no
    # username setting is needed; see app/profile_builder/github_ingest.py)
    github_token: str = ""

    # Gmail send
    gmail_credentials_path: str = "credentials.json"
    gmail_token_path: str = "token.json"
    gmail_sender_address: str = ""

    # Outreach cadence
    # After this many days with no reply, an AWAITING_REPLY application is
    # surfaced on the dashboard as overdue (follow up / escalate / mark
    # NO_RESPONSE - always the owner's call, never automatic).
    no_response_days: int = 14
    # Deliverability (CLAUDE.md section 9): minimum gap between two outgoing
    # sends, so approving several drafts in a row doesn't burst-send.
    send_delay_seconds: int = 60

    # LinkedIn company research (flag-gated external-data provider, CLAUDE.md
    # section 11). Runs stickerdaniel/linkedin-mcp-server over stdio with a
    # dedicated throwaway LinkedIn account - see README "LinkedIn research".
    linkedin_research_enabled: bool = False
    # PyPI name is mcp-server-linkedin (linkedin-mcp-server on PyPI is an
    # unrelated project). Keep @latest and do not pin: LinkedIn changes its
    # page structure often and a pinned version silently starts returning
    # empty data - the server's own docs call this the supported setup.
    linkedin_mcp_command: str = "uvx mcp-server-linkedin@latest"
    # Terms searched in the company's LinkedIn people tab to surface people
    # worth contacting - the unfiltered page returns rank-and-file. One extra
    # page load per term, so trim the list to go faster. These are search data,
    # not UI text: Turkish terms are what catch leadership at Ankara companies.
    linkedin_people_keywords: str = "CEO,CTO,müdür,insan kaynakları,HR,founder"
    # Whole-run backstop across all the calls above (each call has its own
    # fixed ceiling in linkedin_fetch.py). Generous on purpose: a browser
    # loading eight LinkedIn pages is slow.
    linkedin_mcp_timeout_seconds: int = 900
    # Pause between tool calls - rate hygiene even on a throwaway account.
    linkedin_tool_delay_seconds: float = 3.0

    # App server
    app_host: str = "127.0.0.1"
    app_port: int = 8000


@lru_cache
def get_settings() -> Settings:
    return Settings()
