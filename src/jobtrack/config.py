"""Environment-driven settings (pydantic-settings)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://jobtrack:jobtrack@localhost:5432/jobtrack"

    # Discord
    discord_bot_token: str = ""
    discord_channel_id: str = ""
    discord_pace_seconds: float = 1.3  # stay under the 5 msgs / 5 s per-channel bucket
    discord_max_attempts: int = 3

    # Alerting
    sentry_dsn: str = ""
    healthchecks_url: str = ""  # base ping URL, e.g. https://hc-ping.com/<uuid>
    environment: str = "dev"

    # Post-rule filters (see filtering/eligibility.py — not expressible as regex rules)
    max_posting_age_days: int = 30  # 0 disables; postings with no date are always kept
    us_only: bool = True

    # Scheduler
    run_interval_seconds: int = 1800

    # Polite HTTP defaults (per-source overrides live in sources.config.politeness)
    global_concurrency: int = 20
    per_host_concurrency: int = 3
    min_delay_ms: int = 500
    max_delay_ms: int = 1500
    request_timeout_seconds: float = 30.0
    max_retries: int = 3
    backoff_base_seconds: float = 1.0
    backoff_cap_seconds: float = 60.0
    user_agent: str = "jobtrack/0.1 (personal job-listing aggregator)"

    # Circuit breaker: auto-disable a source after this many consecutive failures
    circuit_breaker_threshold: int = 5

    # Named proxy pools for future scraping sources, e.g. {"residential": "http://..."}
    proxy_pools: dict[str, str] = {}

    log_json: bool = True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
