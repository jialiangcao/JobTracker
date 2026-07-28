"""30-minute serve loop with overlap detection, startup recovery, and liveness pings."""

import asyncio
import time

from jobtrack.config import Settings
from jobtrack.db import repo
from jobtrack.db.engine import SessionFactory
from jobtrack.notify import alerts
from jobtrack.observability.logging import get_logger
from jobtrack.pipeline import run_pipeline

log = get_logger(__name__)


async def serve(settings: Settings, session_factory: SessionFactory) -> None:
    async with session_factory() as session:
        recovered = await repo.recover_stuck_runs(session)
        await session.commit()
    if recovered:
        log.warning("recovered stuck runs", count=recovered)

    interval = settings.run_interval_seconds
    while True:
        started = time.monotonic()
        await alerts.ping_healthchecks(settings, "/start")
        try:
            await run_pipeline(settings, session_factory)
        except Exception as exc:
            log.exception("run crashed")
            alerts.capture_exception(exc)
            await alerts.ping_healthchecks(settings, "/fail")
        else:
            await alerts.ping_healthchecks(settings)

        elapsed = time.monotonic() - started
        wait = interval - elapsed
        if wait <= 0:
            # The run overran the tick — something is unhealthy (a slow source, DB, or
            # Discord backlog). Start the next run immediately but make it visible.
            log.warning("run overran interval", elapsed=round(elapsed, 1), interval=interval)
            alerts.capture_message(
                f"jobtrack run took {elapsed:.0f}s, longer than the {interval}s interval"
            )
        else:
            await asyncio.sleep(wait)
