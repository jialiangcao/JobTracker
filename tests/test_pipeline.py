"""End-to-end pipeline tests over sqlite + mocked HTTP."""

import httpx
import respx
from sqlalchemy import select

from jobtrack.config import Settings
from jobtrack.db import repo
from jobtrack.db.engine import SessionFactory
from jobtrack.db.models import Candidate, Run, RunSourceResult, SeenJob, Source
from jobtrack.filtering.rules import SEED_RULES
from jobtrack.pipeline import run_pipeline
from tests.payloads import FULLTIME_JOB, GREENHOUSE_JOB

GH_URL = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"


async def seed(factory: SessionFactory, *, with_rules: bool = True) -> int:
    async with factory() as session:
        source = await repo.add_source(session, "greenhouse", "Acme", {"slug": "acme"})
        if with_rules:
            for name, kind, field, pattern in SEED_RULES:
                await repo.add_rule(session, name, kind, field, pattern)
        await session.commit()
        return source.id


@respx.mock
async def test_full_run_filters_dedups_and_queues(
    settings: Settings, session_factory: SessionFactory
) -> None:
    # No Discord config → candidates must stay queued in the outbox.
    settings = settings.model_copy(update={"discord_bot_token": "", "discord_channel_id": ""})
    await seed(session_factory)
    respx.get(GH_URL).mock(
        return_value=httpx.Response(
            200,
            json={"jobs": [GREENHOUSE_JOB, FULLTIME_JOB]},
            headers={"ETag": 'W/"v1"'},
        )
    )

    summary = await run_pipeline(settings, session_factory)
    assert summary.status == "success"
    assert summary.jobs_fetched == 2
    assert summary.jobs_matched == 1  # full-time job filtered out
    assert summary.jobs_new == 1
    assert summary.jobs_sent == 0

    async with session_factory() as session:
        candidate = await session.scalar(select(Candidate))
        assert candidate is not None
        assert candidate.send_status == "pending"
        assert candidate.title == GREENHOUSE_JOB["title"]
        assert candidate.raw["id"] == GREENHOUSE_JOB["id"]  # raw payload persisted
        run = await session.scalar(select(Run).where(Run.id == summary.run_id))
        assert run is not None
        assert run.status == "success"
        assert run.jobs_new == 1
        source = await session.scalar(select(Source))
        assert source is not None
        assert source.config.get("etag") == 'W/"v1"'  # stored for conditional requests
        assert source.last_success_at is not None

    # Second run: same payload → dedup, nothing new; seen_jobs last_seen bumped.
    summary2 = await run_pipeline(settings, session_factory)
    assert summary2.jobs_new == 0
    async with session_factory() as session:
        seen_rows = list(await session.scalars(select(SeenJob)))
        assert len(seen_rows) == 1  # only matched jobs enter the dedup list
        candidates = list(await session.scalars(select(Candidate)))
        assert len(candidates) == 1


@respx.mock
async def test_source_failure_is_contained_and_breaker_trips(
    settings: Settings, session_factory: SessionFactory
) -> None:
    settings = settings.model_copy(
        update={"discord_bot_token": "", "discord_channel_id": "", "circuit_breaker_threshold": 2}
    )
    source_id = await seed(session_factory)
    respx.get(GH_URL).mock(return_value=httpx.Response(404))

    summary = await run_pipeline(settings, session_factory)
    assert summary.status == "failed"  # the only source failed
    assert summary.sources_failed == 1

    async with session_factory() as session:
        result = await session.scalar(select(RunSourceResult))
        assert result is not None
        assert result.status == "error"
        assert result.http_status == 404
        source = await session.get(Source, source_id)
        assert source is not None
        assert source.consecutive_failures == 1
        assert source.enabled is True

    # Second failure hits the threshold → auto-disable.
    await run_pipeline(settings, session_factory)
    async with session_factory() as session:
        source = await session.get(Source, source_id)
        assert source is not None
        assert source.enabled is False
        assert source.disabled_reason is not None

    # Third run: no enabled sources; run still succeeds and records zero sources.
    summary3 = await run_pipeline(settings, session_factory)
    assert summary3.sources_total == 0
    assert summary3.status == "success"


@respx.mock
async def test_dry_run_writes_nothing(settings: Settings, session_factory: SessionFactory) -> None:
    await seed(session_factory)
    respx.get(GH_URL).mock(return_value=httpx.Response(200, json={"jobs": [GREENHOUSE_JOB]}))

    summary = await run_pipeline(settings, session_factory, dry_run=True)
    assert summary.jobs_new == 1
    assert len(summary.new_jobs) == 1

    async with session_factory() as session:
        assert await session.scalar(select(SeenJob)) is None
        assert await session.scalar(select(Candidate)) is None


@respx.mock
async def test_not_modified_short_circuits(
    settings: Settings, session_factory: SessionFactory
) -> None:
    settings = settings.model_copy(update={"discord_bot_token": "", "discord_channel_id": ""})
    await seed(session_factory)
    respx.get(GH_URL).mock(return_value=httpx.Response(304))

    summary = await run_pipeline(settings, session_factory)
    assert summary.status == "success"
    assert summary.jobs_fetched == 0
    assert summary.sources_ok == 1
