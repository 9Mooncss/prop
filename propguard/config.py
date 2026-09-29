"""Application settings (environment variables, ``PROPGUARD_`` prefix). Secrets are never logged."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PROPGUARD_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/propguard.db"
    data_dir: Path = Path("./data")
    log_level: str = "INFO"
    log_json: bool = True

    # execution safety
    execution_mode: str = "PAPER_ONLY"  # PAPER_ONLY | LIVE_ALLOWED (LIVE also needs per-account approval)
    allow_live: bool = False
    acceptance_marker: Path = Path("./data/acceptance.json")

    # API / UI
    api_token: SecretStr | None = None  # required for mutating endpoints when set
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000

    # monitoring
    monitor_interval_s: int = 3600
    monitor_user_agent: str = "PropGuard-RuleMonitor/0.1 (+self-hosted; contact owner)"
    monitor_min_delay_per_host_s: float = 10.0
    rules_max_age_h: int = 24

    # LLM (optional). Tiers map to model ids via env; architecture never hardcodes a model.
    llm_provider: str = "none"  # none | anthropic
    llm_model_cheap: str = "claude-haiku-4-5"
    llm_model_strong: str = "claude-opus-5-5"
    llm_monthly_budget_usd: float = 5.0
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")

    # notifications
    webhook_url: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    @property
    def live_allowed_env(self) -> bool:
        return self.allow_live and self.execution_mode == "LIVE_ALLOWED"


@lru_cache
def get_settings() -> Settings:
    return Settings()
