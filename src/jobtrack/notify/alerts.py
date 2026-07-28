"""Sentry initialization and healthchecks.io liveness pings."""

import httpx
import sentry_sdk

from jobtrack.config import Settings
from jobtrack.observability.logging import get_logger

log = get_logger(__name__)


def init_sentry(settings: Settings) -> None:
    if not settings.sentry_dsn:
        log.info("sentry disabled (no SENTRY_DSN)")
        return
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        traces_sample_rate=0.0,
    )


def capture_exception(exc: BaseException) -> None:
    sentry_sdk.capture_exception(exc)


def capture_message(message: str, level: str = "warning") -> None:
    sentry_sdk.capture_message(message, level=level)  # type: ignore[arg-type]


async def ping_healthchecks(settings: Settings, suffix: str = "") -> None:
    """suffix: "" = success, "/start" = run started, "/fail" = run failed.
    Never raises — a monitoring outage must not break the pipeline."""
    if not settings.healthchecks_url:
        return
    url = settings.healthchecks_url.rstrip("/") + suffix
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.get(url)
    except httpx.HTTPError as exc:
        log.warning("healthchecks ping failed", suffix=suffix, error=str(exc))
