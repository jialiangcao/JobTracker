"""typer CLI: serve / run-once / db-upgrade / sources / rules."""

import asyncio
import json
from collections.abc import Awaitable, Callable

import typer
from sqlalchemy.ext.asyncio import AsyncSession

from jobtrack import scheduler
from jobtrack.config import get_settings
from jobtrack.db import repo
from jobtrack.db.engine import make_engine, make_session_factory
from jobtrack.filtering.rules import SEED_RULES
from jobtrack.notify import alerts
from jobtrack.observability.logging import setup_logging
from jobtrack.pipeline import run_pipeline

app = typer.Typer(help="jobtrack — job source poller with Discord delivery")
sources_app = typer.Typer(help="Manage sources")
rules_app = typer.Typer(help="Manage filter rules")
app.add_typer(sources_app, name="sources")
app.add_typer(rules_app, name="rules")


def _bootstrap() -> None:
    settings = get_settings()
    setup_logging(json=settings.log_json)
    alerts.init_sentry(settings)


def _with_session(fn: Callable[[AsyncSession], Awaitable[None]]) -> None:
    async def runner() -> None:
        settings = get_settings()
        engine = make_engine(settings)
        try:
            factory = make_session_factory(engine)
            async with factory() as session:
                await fn(session)
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(runner())


@app.command()
def serve() -> None:
    """Run the 30-minute polling loop forever."""
    _bootstrap()

    async def runner() -> None:
        settings = get_settings()
        engine = make_engine(settings)
        try:
            await scheduler.serve(settings, make_session_factory(engine))
        finally:
            await engine.dispose()

    asyncio.run(runner())


@app.command("run-once")
def run_once(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Fetch and filter, but write no dedup state and send nothing."
    ),
) -> None:
    """Execute a single pipeline run."""
    _bootstrap()

    async def runner() -> None:
        settings = get_settings()
        engine = make_engine(settings)
        try:
            summary = await run_pipeline(settings, make_session_factory(engine), dry_run=dry_run)
        finally:
            await engine.dispose()
        typer.echo(
            f"run {summary.run_id}: {summary.status} — fetched {summary.jobs_fetched}, "
            f"matched {summary.jobs_matched}, new {summary.jobs_new}, sent {summary.jobs_sent}"
        )
        if dry_run:
            for job in summary.new_jobs:
                typer.echo(
                    f"  NEW: {job.company} — {job.title} ({job.location or 'n/a'}) {job.url}"
                )

    asyncio.run(runner())


@app.command("db-upgrade")
def db_upgrade() -> None:
    """Apply Alembic migrations up to head."""
    from alembic.config import Config

    from alembic import command

    command.upgrade(Config("alembic.ini"), "head")
    typer.echo("database upgraded to head")


# --- sources ---------------------------------------------------------------


@sources_app.command("add")
def sources_add(
    kind: str = typer.Argument(
        help="greenhouse | lever | ashby | smartrecruiters | workable | scrape"
    ),
    name: str = typer.Argument(help="Display/company name"),
    slug: str = typer.Option("", help="Board token / company slug for ATS kinds"),
    url: str = typer.Option("", help="Careers page URL for scrape sources"),
    adapter: str = typer.Option("", help="Adapter override (e.g. 'fallback')"),
) -> None:
    config: dict[str, object] = {}
    if slug:
        config["slug"] = slug
    if url:
        config["url"] = url
    if adapter:
        config["adapter_override"] = adapter

    async def fn(session: AsyncSession) -> None:
        source = await repo.add_source(session, kind, name, config)
        typer.echo(f"added source #{source.id}: {kind}/{name} {json.dumps(config)}")

    _with_session(fn)


@sources_app.command("list")
def sources_list() -> None:
    async def fn(session: AsyncSession) -> None:
        for s in await repo.list_sources(session):
            flag = "enabled" if s.enabled else f"DISABLED ({s.disabled_reason or 'manual'})"
            typer.echo(
                f"#{s.id} {s.kind}/{s.name} {flag} failures={s.consecutive_failures} "
                f"config={json.dumps(s.config)}"
            )

    _with_session(fn)


@sources_app.command("enable")
def sources_enable(source_id: int) -> None:
    _with_session(lambda s: repo.set_source_enabled(s, source_id, True))
    typer.echo(f"source #{source_id} enabled")


@sources_app.command("disable")
def sources_disable(source_id: int) -> None:
    _with_session(lambda s: repo.set_source_enabled(s, source_id, False))
    typer.echo(f"source #{source_id} disabled")


# --- rules -----------------------------------------------------------------


@rules_app.command("seed")
def rules_seed() -> None:
    """Insert the default CS-internship rules (skips if any rules already exist)."""

    async def fn(session: AsyncSession) -> None:
        existing = await repo.list_rules(session)
        if existing:
            typer.echo(f"{len(existing)} rules already present; not seeding")
            return
        for name, kind, field, pattern in SEED_RULES:
            await repo.add_rule(session, name, kind, field, pattern)
        typer.echo(f"seeded {len(SEED_RULES)} rules")

    _with_session(fn)


@rules_app.command("add")
def rules_add(
    name: str = typer.Argument(help="Group name; 'group:variant' rules OR within the group"),
    kind: str = typer.Argument(help="include | exclude"),
    field: str = typer.Argument(help="title | description | location | title+description"),
    pattern: str = typer.Argument(help="Python regex (case-insensitive)"),
    priority: int = typer.Option(0),
) -> None:
    async def fn(session: AsyncSession) -> None:
        rule = await repo.add_rule(session, name, kind, field, pattern, priority)
        typer.echo(f"added rule #{rule.id}: {name} {kind} {field} /{pattern}/")

    _with_session(fn)


@rules_app.command("list")
def rules_list() -> None:
    async def fn(session: AsyncSession) -> None:
        for r in await repo.list_rules(session):
            flag = "" if r.enabled else " [disabled]"
            typer.echo(f"#{r.id} {r.name} {r.kind} {r.field} /{r.pattern}/{flag}")

    _with_session(fn)


@rules_app.command("enable")
def rules_enable(rule_id: int) -> None:
    _with_session(lambda s: repo.set_rule_enabled(s, rule_id, True))
    typer.echo(f"rule #{rule_id} enabled")


@rules_app.command("disable")
def rules_disable(rule_id: int) -> None:
    _with_session(lambda s: repo.set_rule_enabled(s, rule_id, False))
    typer.echo(f"rule #{rule_id} disabled")


if __name__ == "__main__":
    app()
