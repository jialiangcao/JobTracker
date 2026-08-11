"""One pipeline run: fetch → adapt → filter → dedup → persist → send.

Fetch/adapt/filter run concurrently per source with no DB access. The DB phase is still
serial — one session, one consumer — but it is *streamed*: sources are persisted in
completion order while the rest are still being fetched, so a match found early is sent
minutes before the run ends rather than after it. Discord drains on its own task so a
rate-limited send never stalls the writer; the session is shared under `db_lock`, which
is held around DB work only, never across HTTP.

Per-source failures are contained — the run is 'partial', not 'failed', when only some
sources break. Each source commits individually, so a crash leaves the sources already
consumed advanced and the rest untouched.
"""

import asyncio
import contextlib
import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jobtrack.adapters.registry import get_adapter
from jobtrack.config import Settings
from jobtrack.db import repo
from jobtrack.db.engine import SessionFactory
from jobtrack.db.models import Run, Source
from jobtrack.filtering.eligibility import is_eligible
from jobtrack.filtering.rules import RuleSet, compile_rules, matches
from jobtrack.notify import alerts
from jobtrack.notify.discord import DiscordSender, drain_outbox
from jobtrack.observability.logging import get_logger
from jobtrack.schema import JobPosting
from jobtrack.sources.base import SourceRef
from jobtrack.sources.polite_http import FetchError, PoliteClient
from jobtrack.sources.registry import get_fetcher

log = get_logger(__name__)


@dataclass
class SourceOutcome:
    source_id: int
    status: str  # ok | error
    http_status: int | None = None
    error: str | None = None
    duration_ms: int = 0
    fetched_count: int = 0
    unparseable_count: int = 0
    matched: list[JobPosting] = field(default_factory=list)
    etag: str | None = None
    last_modified: str | None = None
    config_updates: dict[str, object] | None = None


@dataclass
class SourceDelta:
    """One source's contribution to the run summary, folded in after its commit lands."""

    sources_ok: int = 0
    sources_failed: int = 0
    jobs_fetched: int = 0
    jobs_matched: int = 0
    jobs_new: int = 0
    new_jobs: list[JobPosting] = field(default_factory=list)


@dataclass
class RunSummary:
    run_id: int
    status: str
    sources_total: int = 0
    sources_ok: int = 0
    sources_failed: int = 0
    jobs_fetched: int = 0
    jobs_matched: int = 0
    jobs_new: int = 0
    jobs_sent: int = 0
    new_jobs: list[JobPosting] = field(default_factory=list)  # populated for dry runs


async def _process_source(
    ref: SourceRef, client: PoliteClient, rules: RuleSet, settings: Settings
) -> SourceOutcome:
    start = time.monotonic()
    outcome = SourceOutcome(source_id=ref.id, status="ok")
    try:
        result = await get_fetcher(ref.kind).fetch(ref, client)
    except FetchError as exc:
        outcome.status = "error"
        outcome.error = str(exc)
        outcome.http_status = exc.http_status
        outcome.duration_ms = int((time.monotonic() - start) * 1000)
        return outcome
    except Exception as exc:  # unexpected bug — surface in Sentry immediately
        alerts.capture_exception(exc)
        outcome.status = "error"
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.duration_ms = int((time.monotonic() - start) * 1000)
        return outcome

    outcome.http_status = result.http_status
    outcome.etag = result.etag
    outcome.last_modified = result.last_modified
    outcome.config_updates = result.config_updates
    outcome.fetched_count = len(result.postings)

    override = ref.config.get("adapter_override")
    adapter = get_adapter(ref.kind, override if isinstance(override, str) else None)
    for raw in result.postings:
        try:
            job = adapter.map(raw)
        except Exception:
            log.exception("adapter crashed", source=ref.name, kind=ref.kind)
            job = None
        if job is None:
            outcome.unparseable_count += 1
            continue
        if matches(job, rules) and is_eligible(
            job, max_age_days=settings.max_posting_age_days, us_only=settings.us_only
        ):
            outcome.matched.append(job)

    outcome.duration_ms = int((time.monotonic() - start) * 1000)
    log.info(
        "source processed",
        source=ref.name,
        kind=ref.kind,
        fetched=outcome.fetched_count,
        matched=len(outcome.matched),
        unparseable=outcome.unparseable_count,
        not_modified=result.not_modified,
    )
    return outcome


def _ref_config(source: Source, ignore_seen: bool) -> dict[str, object]:
    """Ignoring seen_jobs also has to drop the cached validators: a 304 returns no
    postings at all, so leaving them in would hide the very jobs the flag asks to resurface."""
    config = dict(source.config)
    if ignore_seen:
        config.pop("etag", None)
        config.pop("last_modified", None)
    return config


async def _persist_outcome(
    session: AsyncSession,
    source: Source,
    outcome: SourceOutcome,
    run_id: int,
    settings: Settings,
    *,
    dry_run: bool,
    ignore_seen: bool,
) -> SourceDelta:
    """Write one source's outcome and queue its new matches. Caller owns the commit, and
    folds the returned counters into the run summary only once that commit lands — so a
    source whose write is rolled back does not leave the summary claiming it succeeded."""
    error = outcome.error
    if outcome.status == "ok" and outcome.unparseable_count:
        error = f"{outcome.unparseable_count} unparseable postings"

    delta = SourceDelta()
    new_count = 0
    # A dry run must not disturb the rotation cursor: previewing a slice would otherwise
    # push those sources to the back of the queue and delay their next real poll.
    if not dry_run:
        await repo.mark_source_polled(session, source)
    if outcome.status == "ok":
        delta.sources_ok = 1
        delta.jobs_fetched = outcome.fetched_count
        delta.jobs_matched = len(outcome.matched)
        # A dry run must not store fetch validators: doing so would make the next
        # real run get a 304 and silently skip the jobs the preview just listed.
        await repo.record_source_success(
            session,
            source,
            etag=None if dry_run else outcome.etag,
            last_modified=None if dry_run else outcome.last_modified,
            config_updates=None if dry_run else outcome.config_updates,
        )
        for job in outcome.matched:
            if dry_run:
                if ignore_seen or await repo.find_seen_job(session, source.id, job) is None:
                    new_count += 1
                    delta.new_jobs.append(job)
                continue
            seen, is_new = await repo.upsert_seen_job(session, source.id, job, run_id, matched=True)
            # ignore_seen re-queues known jobs; the seen_jobs row is still upserted
            # (not duplicated) so the candidate has something to hang off.
            if is_new or ignore_seen:
                new_count += 1
                await repo.insert_candidate(session, run_id, seen.id, job)
        delta.jobs_new = new_count
    else:
        delta.sources_failed = 1
        tripped = await repo.record_source_failure(
            session,
            source,
            error or "unknown",
            settings.circuit_breaker_threshold,
            outcome.http_status,
        )
        if tripped:
            message = f"circuit breaker: source {source.kind}/{source.name} disabled ({error})"
            log.error("circuit breaker tripped", source=source.name, error=error)
            alerts.capture_message(message)

    await repo.record_source_result(
        session,
        run_id,
        source.id,
        status=outcome.status,
        http_status=outcome.http_status,
        fetched_count=outcome.fetched_count,
        matched_count=len(outcome.matched),
        new_count=new_count,
        duration_ms=outcome.duration_ms,
        error=error,
    )
    return delta


async def _drain_loop(
    session: AsyncSession,
    sender: DiscordSender,
    settings: Settings,
    stop: asyncio.Event,
    db_lock: asyncio.Lock,
) -> int:
    """Poll the outbox until `stop`, then drain once more. Runs alongside the DB phase so
    matches leave for Discord as they are found; a send failure never aborts the run."""
    total = 0
    while True:
        stopping = stop.is_set()
        try:
            total += await drain_outbox(session, sender, settings, db_lock=db_lock)
        except Exception as exc:  # a broken outbox must not take the run down
            log.exception("outbox drain failed")
            alerts.capture_exception(exc)
        if stopping:
            # One pass ran after stop was set, so nothing queued before it is left behind.
            return total
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.outbox_poll_seconds)


async def run_pipeline(
    settings: Settings,
    session_factory: SessionFactory,
    *,
    dry_run: bool = False,
    max_sources: int | None = None,
    ignore_seen: bool = False,
) -> RunSummary:
    """`max_sources` polls only the first N of the selected sources; `ignore_seen` treats
    every match as new, ignoring seen_jobs. Both are debugging aids for manual runs.

    Which sources a run selects at all is `settings.max_sources_per_run` — the rotation
    budget, not a debugging aid — applied in the query as a least-recently-polled slice."""
    async with session_factory() as session:
        run = await repo.create_run(session)
        source_rows = await repo.get_enabled_sources(session, limit=settings.max_sources_per_run)
        rule_rows = await repo.get_enabled_rules(session)
        await session.commit()
        run_id = run.id

    if max_sources is not None:
        source_rows = source_rows[:max_sources]

    rules = compile_rules(rule_rows)
    if not rules.includes and not rules.excludes:
        log.warning("no enabled filter rules — every fetched job will match")
    refs = [
        SourceRef(id=s.id, kind=s.kind, name=s.name, config=_ref_config(s, ignore_seen))
        for s in source_rows
    ]
    log.info(
        "run started",
        run_id=run_id,
        sources=len(refs),
        dry_run=dry_run,
        ignore_seen=ignore_seen,
    )

    summary = RunSummary(run_id=run_id, status="success", sources_total=len(refs))
    sender: DiscordSender | None = None
    if not dry_run:
        if settings.discord_bot_token and settings.discord_channel_id:
            sender = DiscordSender(settings)
        else:
            log.warning("discord not configured — candidates stay queued in the outbox")

    run_started = time.monotonic()
    first_match_at: float | None = None
    db_lock = asyncio.Lock()

    async with PoliteClient(settings) as client, session_factory() as session:
        # One query warms the identity map so the per-source get() below is a map hit
        # rather than 2000-odd round trips. It is not cached in a dict on purpose: a
        # rolled-back source expires every loaded object, and touching an expired
        # attribute from async code raises MissingGreenlet instead of re-loading.
        # get() re-fetches those properly; every other iteration still costs nothing.
        await session.scalars(select(Source).where(Source.id.in_([r.id for r in refs])))

        stop_draining = asyncio.Event()
        drain_task: asyncio.Task[int] | None = None
        if sender is not None:
            drain_task = asyncio.create_task(
                _drain_loop(session, sender, settings, stop_draining, db_lock)
            )

        tasks = [asyncio.create_task(_process_source(ref, client, rules, settings)) for ref in refs]
        try:
            for finished in asyncio.as_completed(tasks):
                outcome = await finished
                async with db_lock:
                    try:
                        source = await session.get(Source, outcome.source_id)
                        if source is None:  # disabled and deleted mid-run
                            log.warning("source vanished mid-run", source_id=outcome.source_id)
                            continue
                        delta = await _persist_outcome(
                            session,
                            source,
                            outcome,
                            run_id,
                            settings,
                            dry_run=dry_run,
                            ignore_seen=ignore_seen,
                        )
                        await session.commit()
                    except Exception as exc:  # one bad source must not poison the rest
                        await session.rollback()
                        log.exception("persisting source failed", source_id=outcome.source_id)
                        alerts.capture_exception(exc)
                        continue
                summary.sources_ok += delta.sources_ok
                summary.sources_failed += delta.sources_failed
                summary.jobs_fetched += delta.jobs_fetched
                summary.jobs_matched += delta.jobs_matched
                summary.jobs_new += delta.jobs_new
                summary.new_jobs.extend(delta.new_jobs)
                if delta.jobs_new and first_match_at is None:
                    first_match_at = time.monotonic() - run_started
        finally:
            # as_completed leaves the remaining fetches running if the consumer breaks.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if drain_task is not None:
                stop_draining.set()
                summary.jobs_sent = await drain_task

        if summary.sources_failed and summary.sources_ok:
            summary.status = "partial"
        elif summary.sources_failed and not summary.sources_ok and refs:
            summary.status = "failed"

        run_row = await session.get(Run, run_id)
        assert run_row is not None
        run_row.finished_at = repo.utcnow()
        run_row.status = summary.status
        run_row.sources_total = summary.sources_total
        run_row.sources_ok = summary.sources_ok
        run_row.sources_failed = summary.sources_failed
        run_row.jobs_fetched = summary.jobs_fetched
        run_row.jobs_matched = summary.jobs_matched
        run_row.jobs_new = summary.jobs_new
        run_row.jobs_sent = summary.jobs_sent
        await session.commit()

    if sender is not None:
        await sender.aclose()

    log.info(
        "run finished",
        run_id=run_id,
        status=summary.status,
        fetched=summary.jobs_fetched,
        matched=summary.jobs_matched,
        new=summary.jobs_new,
        sent=summary.jobs_sent,
        # Streaming's payoff: how far into the run the first match was queued, versus how
        # long the whole run took. Under the old batch phase the two were always equal.
        duration_s=round(time.monotonic() - run_started, 1),
        first_match_s=None if first_match_at is None else round(first_match_at, 1),
    )
    return summary
