# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Self-hosted job-listing watcher: polls ATS public APIs (Greenhouse, Lever, Ashby, SmartRecruiters, Workable) every 30 minutes, normalizes listings via per-source adapters, filters for CS internships with DB-stored regex rules, dedups against history, and posts new matches as Discord embeds. `implementation_plan.md` is the full design doc (schema, endpoints, rationale).

## Commands

Python 3.13, managed with `uv`. Requires a local Postgres for real runs (see README), but tests use in-memory SQLite — no DB setup needed to run them.

```sh
uv sync                                   # install deps
uv run pytest                             # all tests
uv run pytest tests/test_pipeline.py      # one file
uv run pytest tests/test_pipeline.py::test_name   # one test
uv run ruff check . && uv run ruff format --check .
uv run pyright                            # strict mode
uv run jobtrack run-once --dry-run        # end-to-end against real APIs, no writes/sends
uv run jobtrack db-upgrade                # apply Alembic migrations
```

CLI (`jobtrack …`): `serve` (the 30-min loop), `run-once [--dry-run]`, `sources add/list/enable/disable`, `rules seed/add/list/enable/disable`, `db-upgrade`.

Schema changes require a hand-written Alembic migration in `alembic/versions/` alongside the `db/models.py` edit.

## Architecture

One run flows through `pipeline.py`: fetch → adapt → filter → dedup → persist → send. Fetch/adapt/filter run concurrently per source with no DB access; the DB phase is serial in one session. Per-source failures are contained (run status `partial`, not `failed`).

Two protocol/registry pairs decouple the stages:

- **Fetchers** (`sources/`): `Fetcher.fetch(SourceRef, PoliteClient) -> FetchResult` per ATS kind, resolved by `sources/registry.py`. All HTTP goes through `sources/polite_http.py` — per-host semaphores, jittered pacing, backoff honoring `Retry-After`, ETag/Last-Modified conditional requests (cached in `sources.config`), and per-source politeness overrides. `sources/scrape/` is a stub for future browser-based fetching.
- **Adapters** (`adapters/`): `Adapter.map(RawPosting) -> JobPosting | None` (None = unparseable, counted not fatal), resolved from `source.kind` by `adapters/registry.py`; `config.adapter_override` on a source wins. `adapters/fallback.py` is a heuristic key-search adapter used for unknown kinds.

`RawPosting` is the fetcher→adapter boundary type; `JobPosting` (both in `schema.py`) is the internal normalized schema everything downstream consumes.

Behavior that lives in the DB, not code:

- **Filter rules** (`filter_rules` table, applied by `filtering/rules.py`): rules sharing a name prefix (`season:*`) form a group; a job must match every enabled include group (OR within a group) and no exclude rule. Change criteria via `jobtrack rules`, not deploys.
- **Sources** (`sources` table): `config` jsonb holds slug/URL, adapter override, politeness, cached ETags. A source failing `circuit_breaker_threshold` (5) consecutive runs is auto-disabled with a Sentry event.
- **Discord outbox** (`candidates` table): matches are inserted `pending`, then `notify/discord.py` drains the outbox serially with rate-limit pacing. A crash or Discord outage never drops or duplicates a notification — leftovers send next run.

Dedup (`filtering/dedup.py` + `seen_jobs` table) keys on `external_id`, then normalized canonical URL, then content hash.

`scheduler.py` wraps the pipeline in the 30-min loop with an overlap guard and healthchecks.io start/success/fail pings; startup marks stuck `running` runs as `failed`.

## Tests

`tests/conftest.py` provides a `Settings` fixture (zeroed delays) and an in-memory aiosqlite `session_factory` with tables created from the ORM metadata (migrations are not exercised). HTTP is mocked with `respx`; `tests/payloads.py` holds captured real ATS payloads — adapter tests run against these, including with the kind deliberately mislabeled to exercise the fallback adapter. pytest-asyncio is in auto mode, so async tests need no marker.
