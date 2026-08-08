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
    anthropic_model: str = "claude-haiku-4-5"  # cheapest tier - this app's extraction/drafting tasks don't need more

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

    # App server
    app_host: str = "127.0.0.1"
    app_port: int = 8000


@lru_cache
def get_settings() -> Settings:
    return Settings()
