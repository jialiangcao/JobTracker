"""Query layer: run bookkeeping, source management, dedup upserts, and the send outbox."""

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from jobtrack.db.models import Candidate, FilterRule, Run, RunSourceResult, SeenJob, Source
from jobtrack.filtering.dedup import canonical_url, content_hash
from jobtrack.schema import JobPosting
from jobtrack.sources_file import SourceSpec, merge_config


def utcnow() -> datetime:
    return datetime.now(UTC)


# Statuses that mean "come back later", not "this board is gone" — see record_source_failure.
_THROTTLE_STATUSES = frozenset({429, 503})


# --- sources ---------------------------------------------------------------


async def get_enabled_sources(session: AsyncSession, limit: int | None = None) -> list[Source]:
    """Enabled sources, least-recently-polled first (never-polled ahead of everything).

    With more boards than one interval can politely poll, `limit` takes a slice and the
    ordering rotates through the rest on later runs: every source is still reached, just
    every few runs instead of every run. Ties break on id so the order is deterministic.
    """
    query = (
        select(Source)
        .where(Source.enabled)
        .order_by(Source.last_polled_at.is_(None).desc(), Source.last_polled_at, Source.id)
    )
    if limit is not None and limit > 0:
        query = query.limit(limit)
    return list(await session.scalars(query))


async def add_source(session: AsyncSession, kind: str, name: str, config: dict[str, Any]) -> Source:
    source = Source(kind=kind, name=name, config=config, created_at=utcnow())
    session.add(source)
    await session.flush()
    return source


async def list_sources(session: AsyncSession) -> list[Source]:
    return list(await session.scalars(select(Source).order_by(Source.id)))


async def find_source(session: AsyncSession, kind: str, name: str) -> Source | None:
    return await session.scalar(select(Source).where(Source.kind == kind, Source.name == name))


async def sync_source(session: AsyncSession, spec: SourceSpec) -> str:
    """Reconcile one declared source into the DB. Returns 'added'|'updated'|'unchanged'.

    Config is merged so app-written keys (etag, last_modified) survive; `enabled` is only
    applied when the file states it, so syncing never silently re-enables a source the
    circuit breaker disabled."""
    source = await find_source(session, spec.kind, spec.name)
    if source is None:
        source = await add_source(session, spec.kind, spec.name, dict(spec.config))
        if spec.enabled is False:
            source.enabled = False
        return "added"

    changed = False
    merged = merge_config(source.config, spec.config)
    if merged != source.config:
        source.config = merged
        changed = True
    if spec.enabled is not None and spec.enabled != source.enabled:
        source.enabled = spec.enabled
        if spec.enabled:
            source.consecutive_failures = 0
            source.disabled_reason = None
        changed = True
    return "updated" if changed else "unchanged"


async def set_source_enabled(session: AsyncSession, source_id: int, enabled: bool) -> None:
    values: dict[str, Any] = {"enabled": enabled}
    if enabled:
        values |= {"consecutive_failures": 0, "disabled_reason": None}
    await session.execute(update(Source).where(Source.id == source_id).values(**values))


async def reenable_auto_disabled(session: AsyncSession) -> int:
    """Re-enable every source the circuit breaker disabled, clearing its streak.

    Scoped to breaker trips (`disabled_reason` set) so sources disabled by hand or by
    `sources sync --prune` stay off. Returns how many were revived."""
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(Source)
            .where(~Source.enabled, Source.disabled_reason.is_not(None))
            .values(enabled=True, consecutive_failures=0, disabled_reason=None)
        ),
    )
    return result.rowcount


async def mark_source_polled(session: AsyncSession, source: Source) -> None:
    """Advance the rotation cursor. Called for failures too — a board that errors must
    not monopolize the next run's budget by staying permanently least-recently-polled."""
    source.last_polled_at = utcnow()


async def record_source_success(
    session: AsyncSession,
    source: Source,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
) -> None:
    source.consecutive_failures = 0
    source.last_success_at = utcnow()
    if etag is not None:
        source.config["etag"] = etag
        flag_modified(source, "config")
    if last_modified is not None:
        source.config["last_modified"] = last_modified
        flag_modified(source, "config")


async def record_source_failure(
    session: AsyncSession,
    source: Source,
    error: str,
    breaker_threshold: int,
    http_status: int | None = None,
) -> bool:
    """Increment the failure streak; trip the circuit breaker at the threshold.
    Returns True if the source was auto-disabled by this failure.

    Throttling does not count. A 429 means *we* polled too fast and a 503 means the
    provider is shedding load — neither says the board is gone, and counting them let a
    pacing bug auto-disable half the fleet. The streak is left untouched (not reset), so
    a source alternating between throttled and genuinely broken still trips eventually."""
    if http_status in _THROTTLE_STATUSES:
        return False
    source.consecutive_failures += 1
    if source.consecutive_failures >= breaker_threshold:
        source.enabled = False
        source.disabled_reason = (
            f"auto-disabled after {source.consecutive_failures} consecutive failures; last: {error}"
        )
        return True
    return False


# --- runs ------------------------------------------------------------------


async def create_run(session: AsyncSession) -> Run:
    run = Run(started_at=utcnow(), status="running")
    session.add(run)
    await session.flush()
    return run


async def recover_stuck_runs(session: AsyncSession) -> int:
    """Mark runs left in 'running' (e.g. by a crash/restart) as failed. Returns count."""
    stuck = list(await session.scalars(select(Run).where(Run.status == "running")))
    for run in stuck:
        run.status = "failed"
        run.finished_at = utcnow()
        run.error = "stuck in running at startup"
    return len(stuck)


async def record_source_result(
    session: AsyncSession,
    run_id: int,
    source_id: int,
    *,
    status: str,
    http_status: int | None = None,
    fetched_count: int | None = None,
    matched_count: int | None = None,
    new_count: int | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
) -> None:
    session.add(
        RunSourceResult(
            run_id=run_id,
            source_id=source_id,
            status=status,
            http_status=http_status,
            fetched_count=fetched_count,
            matched_count=matched_count,
            new_count=new_count,
            duration_ms=duration_ms,
            error=error,
        )
    )


# --- dedup + candidates ----------------------------------------------------


async def find_seen_job(session: AsyncSession, source_id: int, job: JobPosting) -> SeenJob | None:
    """Dedup lookup: external_id first, then canonical URL, then content hash (the hash
    only when the source exposes no stable id)."""
    url = canonical_url(job.url)
    seen: SeenJob | None = None
    if job.external_id is not None:
        seen = await session.scalar(
            select(SeenJob).where(
                SeenJob.source_id == source_id, SeenJob.external_id == job.external_id
            )
        )
    if seen is None:
        seen = await session.scalar(
            select(SeenJob).where(SeenJob.source_id == source_id, SeenJob.canonical_url == url)
        )
    if seen is None and job.external_id is None:
        chash = content_hash(job.title, job.company, job.location)
        seen = await session.scalar(
            select(SeenJob).where(SeenJob.source_id == source_id, SeenJob.content_hash == chash)
        )
    return seen


async def upsert_seen_job(
    session: AsyncSession,
    source_id: int,
    job: JobPosting,
    run_id: int,
    matched: bool,
) -> tuple[SeenJob, bool]:
    """Bump last_seen_at on dedup hits, insert on miss. Returns (row, is_new)."""
    now = utcnow()
    seen = await find_seen_job(session, source_id, job)
    if seen is not None:
        seen.last_seen_at = now
        return seen, False

    seen = SeenJob(
        source_id=source_id,
        external_id=job.external_id,
        canonical_url=canonical_url(job.url),
        content_hash=content_hash(job.title, job.company, job.location),
        matched=matched,
        first_seen_at=now,
        last_seen_at=now,
        first_seen_run_id=run_id,
    )
    session.add(seen)
    await session.flush()
    return seen, True


async def insert_candidate(
    session: AsyncSession, run_id: int, seen_job_id: int, job: JobPosting
) -> Candidate:
    candidate = Candidate(
        run_id=run_id,
        seen_job_id=seen_job_id,
        title=job.title,
        company=job.company,
        location=job.location,
        url=job.url,
        posted_at=job.posted_at,
        raw=job.raw,
    )
    session.add(candidate)
    await session.flush()
    return candidate


async def pending_candidates(session: AsyncSession, max_attempts: int) -> list[Candidate]:
    result = await session.scalars(
        select(Candidate)
        .where(Candidate.send_status == "pending", Candidate.send_attempts < max_attempts)
        .order_by(Candidate.id)
    )
    return list(result)


async def mark_candidate_sent(
    session: AsyncSession, candidate: Candidate, message_id: str | None
) -> None:
    candidate.send_status = "sent"
    candidate.sent_at = utcnow()
    candidate.discord_message_id = message_id
    candidate.send_attempts += 1


async def mark_candidate_failed(
    session: AsyncSession, candidate: Candidate, max_attempts: int
) -> None:
    candidate.send_attempts += 1
    if candidate.send_attempts >= max_attempts:
        candidate.send_status = "failed"


# --- filter rules ----------------------------------------------------------


async def get_enabled_rules(session: AsyncSession) -> list[FilterRule]:
    result = await session.scalars(
        select(FilterRule).where(FilterRule.enabled).order_by(FilterRule.priority, FilterRule.id)
    )
    return list(result)


async def add_rule(
    session: AsyncSession, name: str, kind: str, field: str, pattern: str, priority: int = 0
) -> FilterRule:
    rule = FilterRule(name=name, kind=kind, field=field, pattern=pattern, priority=priority)
    session.add(rule)
    await session.flush()
    return rule


async def list_rules(session: AsyncSession) -> list[FilterRule]:
    return list(await session.scalars(select(FilterRule).order_by(FilterRule.id)))


async def set_rule_enabled(session: AsyncSession, rule_id: int, enabled: bool) -> None:
    await session.execute(
        update(FilterRule).where(FilterRule.id == rule_id).values(enabled=enabled)
    )
