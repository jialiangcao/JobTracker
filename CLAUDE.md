# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Self-hosted job-listing watcher: polls ATS public APIs (Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Workday) every 45 minutes, normalizes listings via per-source adapters, filters for CS internships with DB-stored regex rules, dedups against history, and posts new matches as Discord embeds. `implementation_plan.md` is the full design doc (schema, endpoints, rationale).

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

CLI (`jobtrack …`): `serve` (the poll loop), `run-once [--dry-run] [-n/--max-sources N] [--ignore-seen]`, `sources add/list/enable/disable`, `rules seed/add/list/enable/disable`, `db-upgrade`.

Schema changes require a hand-written Alembic migration in `alembic/versions/` alongside the `db/models.py` edit.

## Architecture

One run flows through `pipeline.py`: fetch → adapt → filter → dedup → persist → send. Fetch/adapt/filter run concurrently per source with no DB access; the DB phase is serial in one session. Per-source failures are contained (run status `partial`, not `failed`).

A run polls the `MAX_SOURCES_PER_RUN` least-recently-polled enabled sources (`sources.last_polled_at`, advanced on every non-dry poll), so the fleet can be larger than one interval will politely fetch. It defaults to `0` (no cap — every enabled source, every run), and `RUN_INTERVAL_SECONDS` is what's sized to fit a full sweep; see the comment on the setting for why a count-based cap is the wrong knob to reach for.

Two protocol/registry pairs decouple the stages:

- **Fetchers** (`sources/`): `Fetcher.fetch(SourceRef, PoliteClient) -> FetchResult` per ATS kind, resolved by `sources/registry.py`. All HTTP goes through `sources/polite_http.py` — per-host semaphores, jittered pacing, backoff honoring `Retry-After`, ETag/Last-Modified conditional requests (cached in `sources.config`), and per-source politeness overrides. Providers in `_SHARED_LIMIT_DOMAINS` (Workday) pace and cap as one group across all their subdomains, since they rate-limit per client IP no matter which tenant is addressed; `_GROUP_POLITENESS` sets the rate for those groups and for single hosts serving the whole fleet (`apply.workable.com`). Note that Workday enforces its real ceiling with a bot-mitigation challenge served as **HTTP 200 with an HTML body**, not a 429 — rate probes cannot see it, so treat a JSON parse failure there as a throttling signal. `sources/scrape/` is a stub for future browser-based fetching.
- **Adapters** (`adapters/`): `Adapter.map(RawPosting) -> JobPosting | None` (None = unparseable, counted not fatal), resolved from `source.kind` by `adapters/registry.py`; `config.adapter_override` on a source wins. `adapters/fallback.py` is a heuristic key-search adapter used for unknown kinds.

`RawPosting` is the fetcher→adapter boundary type; `JobPosting` (both in `schema.py`) is the internal normalized schema everything downstream consumes.

Behavior that lives in the DB, not code:

- **Filter rules** (`filter_rules` table, applied by `filtering/rules.py`): rules sharing a name prefix (`season:*`) form a group; a job must match every enabled include group (OR within a group) and no exclude rule. Change criteria via `jobtrack rules`, not deploys.
- **Sources** (`sources` table): `config` jsonb holds slug/URL, adapter override, politeness, and what fetches write back — cached ETags, plus Workday's discovered facet (`sources_file.RUNTIME_KEYS`; a fetcher returns these as `FetchResult.config_updates`, and a `None` value retracts a key). Since Workday shares one pacing clock fleet-wide, requests-per-board (~2.0 with the cache warm) sets the run's wall clock, and the facet cache is what keeps it there — it buys the discovery probe once a week instead of once a run. A source failing `circuit_breaker_threshold` (5) consecutive runs is auto-disabled with a Sentry event — except on 429/503, which mean our pacing is wrong, not that the board is gone. `jobtrack sources reenable-all` undoes breaker trips in bulk.
- **Discord outbox** (`candidates` table): matches are inserted `pending`, then `notify/discord.py` drains the outbox serially with rate-limit pacing. A crash or Discord outage never drops or duplicates a notification — leftovers send next run.

Dedup (`filtering/dedup.py` + `seen_jobs` table) keys on `external_id`, then normalized canonical URL, then content hash.

`scheduler.py` wraps the pipeline in the `run_interval_seconds` loop with an overlap guard and healthchecks.io start/success/fail pings; startup marks stuck `running` runs as `failed`.

## Tests

`tests/conftest.py` provides a `Settings` fixture (zeroed delays) and an in-memory aiosqlite `session_factory` with tables created from the ORM metadata (migrations are not exercised). HTTP is mocked with `respx`; `tests/payloads.py` holds captured real ATS payloads — adapter tests run against these, including with the kind deliberately mislabeled to exercise the fallback adapter. pytest-asyncio is in auto mode, so async tests need no marker.
