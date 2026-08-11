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
    outbox_poll_seconds: float = 2.0  # how often the in-run drain checks for new matches

    # Alerting
    sentry_dsn: str = ""
    healthchecks_url: str = ""  # base ping URL, e.g. https://hc-ping.com/<uuid>
    environment: str = "dev"

    # Post-rule filters (see filtering/eligibility.py — not expressible as regex rules)
    max_posting_age_days: int = 30  # 0 disables; postings with no date are always kept
    us_only: bool = True

    # Scheduler. Sized from the busiest pacing clock, not from how fresh we'd like the
    # results: a full-fleet sweep is bounded by Workday at ~1290s (1962 boards x ~2.0
    # requests x 325ms), so 2700 leaves roughly 2x headroom for fleet growth and for the
    # occasional run that re-probes facets. Shortening this below ~1600s means runs start
    # overlapping the sweep they are meant to replace — check the pacing math in
    # polite_http._GROUP_POLITENESS before lowering it.
    run_interval_seconds: int = 2700

    # Polite HTTP defaults (per-source overrides live in sources.config.politeness)
    #
    # The delay is what sets throughput: _pace() spaces request *starts* on a host, so a
    # run's wall clock is roughly (sources on the busiest host x average delay) — with
    # ~1100 Greenhouse boards, 500-1500ms meant an 18-minute run. The concurrency caps
    # only bound what is in flight; they are sized so a host with slow responses does not
    # throttle the pacing clock (in-flight settles around latency / delay).
    global_concurrency: int = 40
    per_host_concurrency: int = 8
    min_delay_ms: int = 150
    max_delay_ms: int = 400
    request_timeout_seconds: float = 30.0
    max_retries: int = 3
    backoff_base_seconds: float = 1.0
    backoff_cap_seconds: float = 60.0
    user_agent: str = "jobtrack/0.1 (personal job-listing aggregator)"

    # Rotation budget: poll at most this many sources per run, least-recently-polled
    # first, so the fleet can outgrow what one interval will politely fetch. 0 = no cap
    # (every enabled source every run), which is only safe while the pacing math above
    # says the busiest host fits inside run_interval_seconds.
    #
    # Prefer widening run_interval_seconds to setting this. A count-based cap slices a
    # queue ordered by last_polled_at, and that order stays correlated with `kind` (it
    # inherits sources.toml's grouping), so a slice is never a representative sample of
    # the fleet: at 4000 it put every Workday board in the same slice and made every third
    # run take 54 minutes while the other two took 8. If it is ever needed again, size it
    # so the expensive kinds are spread across slices rather than concentrated in one.
    max_sources_per_run: int = 0

    # Circuit breaker: auto-disable a source after this many consecutive failures.
    # Throttling (429/503) is exempt — see repo.record_source_failure.
    circuit_breaker_threshold: int = 5

    # Named proxy pools for future scraping sources, e.g. {"residential": "http://..."}
    proxy_pools: dict[str, str] = {}

    log_json: bool = True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
