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
    gemini_model: str = "gemini-1.5-flash"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-fable-5"

    # Storage
    database_url: str = "sqlite:///data/app.db"

    # GitHub ingestion
    github_username: str = ""
    github_token: str = ""

    # Gmail send
    gmail_credentials_path: str = "credentials.json"
    gmail_token_path: str = "token.json"
    gmail_sender_address: str = ""

    # App server
    app_host: str = "127.0.0.1"
    app_port: int = 8000


@lru_cache
def get_settings() -> Settings:
    return Settings()
