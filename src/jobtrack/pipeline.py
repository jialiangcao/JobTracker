"""One pipeline run: fetch → adapt → filter → dedup → persist → send.

Fetch/adapt/filter run concurrently per source with no DB access; the DB phase is
serial (one session). Per-source failures are contained — the run is 'partial', not
'failed', when only some sources break.
"""

import asyncio
import time
from dataclasses import dataclass, field

from sqlalchemy import select

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


async def run_pipeline(
    settings: Settings,
    session_factory: SessionFactory,
    *,
    dry_run: bool = False,
    max_sources: int | None = None,
    ignore_seen: bool = False,
) -> RunSummary:
    """`max_sources` polls only the first N enabled sources (by id); `ignore_seen` treats
    every match as new, ignoring seen_jobs. Both are debugging aids for manual runs."""
    async with session_factory() as session:
        run = await repo.create_run(session)
        source_rows = await repo.get_enabled_sources(session)
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

    async with PoliteClient(settings) as client:
        outcomes = await asyncio.gather(
            *(_process_source(ref, client, rules, settings) for ref in refs)
        )

    summary = RunSummary(run_id=run_id, status="success", sources_total=len(refs))
    async with session_factory() as session:
        rows = await session.scalars(select(Source).where(Source.id.in_([r.id for r in refs])))
        by_id = {s.id: s for s in rows}

        for outcome in outcomes:
            source = by_id[outcome.source_id]
            error = outcome.error
            if outcome.status == "ok" and outcome.unparseable_count:
                error = f"{outcome.unparseable_count} unparseable postings"

            new_count = 0
            if outcome.status == "ok":
                summary.sources_ok += 1
                summary.jobs_fetched += outcome.fetched_count
                summary.jobs_matched += len(outcome.matched)
                # A dry run must not store fetch validators: doing so would make the next
                # real run get a 304 and silently skip the jobs the preview just listed.
                await repo.record_source_success(
                    session,
                    source,
                    etag=None if dry_run else outcome.etag,
                    last_modified=None if dry_run else outcome.last_modified,
                )
                for job in outcome.matched:
                    if dry_run:
                        if ignore_seen or await repo.find_seen_job(session, source.id, job) is None:
                            new_count += 1
                            summary.new_jobs.append(job)
                        continue
                    seen, is_new = await repo.upsert_seen_job(
                        session, source.id, job, run_id, matched=True
                    )
                    # ignore_seen re-queues known jobs; the seen_jobs row is still upserted
                    # (not duplicated) so the candidate has something to hang off.
                    if is_new or ignore_seen:
                        new_count += 1
                        await repo.insert_candidate(session, run_id, seen.id, job)
                summary.jobs_new += new_count
            else:
                summary.sources_failed += 1
                tripped = await repo.record_source_failure(
                    session, source, error or "unknown", settings.circuit_breaker_threshold
                )
                if tripped:
                    message = (
                        f"circuit breaker: source {source.kind}/{source.name} disabled ({error})"
                    )
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
        await session.commit()

        if not dry_run and settings.discord_bot_token and settings.discord_channel_id:
            sender = DiscordSender(settings)
            try:
                summary.jobs_sent = await drain_outbox(session, sender, settings)
            finally:
                await sender.aclose()
        elif not dry_run:
            log.warning("discord not configured — candidates stay queued in the outbox")

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

    log.info(
        "run finished",
        run_id=run_id,
        status=summary.status,
        fetched=summary.jobs_fetched,
        matched=summary.jobs_matched,
        new=summary.jobs_new,
        sent=summary.jobs_sent,
    )
    return summary
