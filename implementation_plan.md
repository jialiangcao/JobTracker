# jobtrack — Implementation Plan

A self-hosted service that polls job sources every 30 minutes, normalizes listings into an
internal schema via per-source adapters, filters for CS internships (winter/spring/summer) with
DB-configurable regex rules, dedups against history, and delivers new matches as Discord embeds —
with per-run bookkeeping, Sentry error tracking, and a healthchecks.io dead-man switch.

## 1. Decisions (locked in)

| Area | Choice |
|---|---|
| Language / tooling | Python 3.13, `uv` (deps + venv), `ruff` (lint + format), `pyright` strict |
| Runtime model | Single async process; plain `asyncio` scheduler loop (30-min tick, overlap guard) |
| HTTP | `httpx` (async, HTTP/2) with a shared "polite client" layer |
| Validation / schema | Pydantic v2 (internal job schema, adapter outputs, settings via `pydantic-settings`) |
| Database | Postgres 17, SQLAlchemy 2.0 async + `asyncpg`, Alembic migrations |
| v1 sources | ATS public APIs: Greenhouse, Lever, Ashby (+ SmartRecruiters, Workable stretch) |
| Future sources | Careers-page scraping + big boards — infra hooks built now (see §5), impl later |
| Discord | Bot token, REST-only via `httpx` (no gateway). One embed per job to a channel |
| Alerting | Sentry (exceptions) + healthchecks.io (liveness dead-man switch) |
| Logging | `structlog`, JSON to stdout (docker captures; `max-size` log rotation) |
| Hosting | Hetzner CX22 (2 vCPU / 4 GB), Ubuntu 24.04, Docker Compose (app + postgres) |
| Tests | `pytest` + `pytest-asyncio`, `respx` for httpx mocking |
| Management | Small `typer` CLI: `run-once`, `serve`, `sources add/list/disable`, `rules ...` |

Why REST-only Discord instead of `discord.py`: we only POST messages; a gateway connection adds a
long-lived websocket to babysit for zero benefit. If interactivity is wanted later (reactions to
mark applied, slash commands), swap the sender for a `discord.py` bot — the send queue interface
stays the same.

## 2. Repository layout

```
jobtrack/
├── pyproject.toml            # uv-managed; ruff + pyright config inline
├── uv.lock
├── .python-version           # 3.13
├── Dockerfile                # multi-stage uv build, non-root user
├── docker-compose.yml        # app + postgres:17-alpine, volumes, healthchecks
├── .env.example
├── alembic/                  # migrations
├── src/jobtrack/
│   ├── main.py               # typer CLI entrypoint (serve / run-once / sources / rules)
│   ├── config.py             # pydantic-settings (env-driven)
│   ├── scheduler.py          # 30-min loop, overlap guard, healthchecks pings
│   ├── pipeline.py           # one run: fetch → adapt → filter → dedup → persist → send
│   ├── schema.py             # JobPosting internal schema + RawPosting
│   ├── db/
│   │   ├── engine.py
│   │   ├── models.py         # ORM: sources, runs, run_source_results, seen_jobs, candidates, filter_rules
│   │   └── repo.py           # query layer (dedup checks, run recording, outbox)
│   ├── sources/
│   │   ├── base.py           # Fetcher protocol: (source_row) -> list[RawPosting]
│   │   ├── polite_http.py    # per-host limiter, jitter, backoff, UA, circuit breaker
│   │   ├── greenhouse.py / lever.py / ashby.py / smartrecruiters.py / workable.py
│   │   └── scrape/           # future: BrowserFetcher (Playwright) — stub + interface only in v1
│   ├── adapters/
│   │   ├── base.py           # Adapter protocol: RawPosting -> JobPosting | None
│   │   ├── registry.py       # source.kind → adapter, with per-source override in source.config
│   │   ├── fallback.py       # generic heuristic adapter (see §6)
│   │   └── greenhouse.py / lever.py / ashby.py / ...
│   ├── filtering/
│   │   ├── rules.py          # load + compile filter_rules from DB, apply include/exclude
│   │   └── dedup.py          # external_id / canonical_url / content_hash strategy
│   ├── notify/
│   │   ├── discord.py        # DB-backed outbox → rate-limit-aware sender, embed builder
│   │   └── alerts.py         # sentry init/capture helpers, healthchecks pings
│   └── observability/logging.py
└── tests/
```

## 3. Internal job schema

```python
class JobPosting(BaseModel):
    external_id: str | None  # ATS job id when available
    title: str
    company: str
    url: str  # canonical apply/detail URL
    location: str | None
    remote: bool | None
    description: str | None  # plain text (HTML stripped) — regex runs against this
    posted_at: datetime | None
    updated_at: datetime | None
    department: str | None
    employment_type: str | None  # intern / full-time / ... when the source exposes it
    compensation: str | None  # Ashby exposes this; others usually not
    source_kind: str  # greenhouse | lever | ashby | ...
    raw: dict[str, Any]  # original payload, carried through for candidate persistence
```

`RawPosting` is just `{source_id, source_kind, payload: dict, fetched_at}` — the boundary type
between fetchers and adapters.

## 4. Database schema

```sql
sources (
  id            bigserial PK,
  kind          text NOT NULL,          -- 'greenhouse' | 'lever' | 'ashby' | 'smartrecruiters'
                                        -- | 'workable' | 'scrape' | 'board'
  name          text NOT NULL,          -- display name, e.g. company
  config        jsonb NOT NULL,         -- {board_token | company_slug | url, adapter_override?,
                                        --  politeness?: {max_concurrency, min_delay_ms},
                                        --  needs_browser?: bool, proxy_pool?: text}
  enabled       boolean NOT NULL DEFAULT true,
  consecutive_failures int NOT NULL DEFAULT 0,
  disabled_reason text,                 -- set when circuit breaker trips
  last_success_at timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (kind, name)
)

runs (
  id            bigserial PK,
  started_at    timestamptz NOT NULL,
  finished_at   timestamptz,
  status        text NOT NULL,          -- 'running' | 'success' | 'partial' | 'failed'
  sources_total int, sources_ok int, sources_failed int,
  jobs_fetched  int, jobs_matched int, jobs_new int, jobs_sent int,
  error         text                    -- run-level (not per-source) failure detail
)

run_source_results (
  id            bigserial PK,
  run_id        bigint FK → runs,
  source_id     bigint FK → sources,
  status        text NOT NULL,          -- 'ok' | 'error' | 'skipped'
  http_status   int,
  fetched_count int, matched_count int, new_count int,
  duration_ms   int,
  error         text
)

seen_jobs (                             -- the dedup list; retained indefinitely (tiny data)
  id            bigserial PK,
  source_id     bigint FK → sources,
  external_id   text,                   -- dedup key #1 when present
  canonical_url text NOT NULL,          -- dedup key #2 (normalized: lowercase host, strip
                                        -- tracking params, trailing slash)
  content_hash  text NOT NULL,          -- sha256(title|company|location) — key #3 fallback
  matched       boolean NOT NULL,       -- passed filters when first seen
  first_seen_at timestamptz NOT NULL,
  last_seen_at  timestamptz NOT NULL,   -- bumped every run it still appears; enables later expiry
  first_seen_run_id bigint FK → runs,
  UNIQUE (source_id, external_id),
  UNIQUE (source_id, canonical_url)
)

candidates (                            -- final matches; doubles as the Discord send outbox
  id            bigserial PK,
  run_id        bigint FK → runs,
  seen_job_id   bigint FK → seen_jobs,
  title text, company text, location text, url text, posted_at timestamptz,
  raw           jsonb NOT NULL,         -- full original payload, as required
  send_status   text NOT NULL DEFAULT 'pending',  -- 'pending' | 'sent' | 'failed'
  send_attempts int NOT NULL DEFAULT 0,
  sent_at       timestamptz,
  discord_message_id text
)

filter_rules (
  id       bigserial PK,
  name     text NOT NULL,
  kind     text NOT NULL,               -- 'include' | 'exclude'
  field    text NOT NULL,               -- 'title' | 'description' | 'location' | 'title+description'
  pattern  text NOT NULL,               -- Python regex, compiled with re.IGNORECASE
  enabled  boolean NOT NULL DEFAULT true,
  priority int NOT NULL DEFAULT 0
)
```

**Filter semantics:** a job matches iff *every* enabled include-group matches and *no* enabled
exclude rule matches. Includes with the same `name` prefix (e.g. `season:*`) are OR'd within the
group, groups are AND'd. Seed rules:

- `role` (include, title): `\bintern(ship)?\b|\bco[-\s]?op\b`
- `cs` (include, title): `software|swe\b|developer|engineer|data|machine\s*learning|\bml\b|\bai\b|security|infra|backend|front[-\s]?end|full[-\s]?stack|mobile|devops|sre\b|platform|quant`
- `season` (include, title+description): `winter|spring|summer|20(2[6-9])`
- `not-hardware` (exclude, title): `mechanical|electrical\s+eng|civil\b|chemical\b|hardware\b`

Rules live in the DB so criteria changes never need a deploy (`jobtrack rules add/list/disable`).

## 5. Fetch layer & anti-blocking (research summary)

v1 endpoints (all unauthenticated JSON, descriptions included in one call — no detail fetches):

- Greenhouse: `GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true`
- Lever: `GET https://api.lever.co/v0/postings/{company}?mode=json`
- Ashby: `GET https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true`
- SmartRecruiters: `GET https://api.smartrecruiters.com/v1/companies/{company}/postings`
- Workable: `GET https://apply.workable.com/api/v1/widget/accounts/{account}`

None of these publish hard rate limits for the public endpoints; guidance is "poll politely on a
schedule, don't hammer." The `polite_http` layer enforces, for every source kind (values are
defaults, overridable per source via `config.politeness`):

1. **Per-host concurrency cap** — semaphore keyed by netloc, default 3; global cap 20.
2. **Per-host pacing with jitter** — min inter-request delay 500–1500 ms randomized, so one run
   never bursts a host even when many companies share `boards-api.greenhouse.io`.
3. **Backoff** — exponential + jitter on 429/5xx/timeouts (3 attempts); honor `Retry-After` and
   `X-RateLimit-*` headers when present.
4. **Honest headers** — stable realistic User-Agent for the API sources (rotation is a
   scraping-mode concern); `Accept-Encoding` compression on.
5. **Conditional requests** — cache `ETag`/`Last-Modified` per source in `sources.config` and send
   `If-None-Match`; 304s cost nothing and cut block risk.
6. **Circuit breaker** — `consecutive_failures` incremented per source; at N=5 the source is
   auto-disabled with `disabled_reason` + a Sentry event, so one dead board can't burn requests
   (and alerts) every 30 minutes forever.
7. **In-run URL dedup / no refetch** — a source is fetched at most once per run.

**Hooks for future scraping / big boards (built in v1, implemented later):**
- `Fetcher` protocol takes the whole source row; `kind='scrape'` routes to a `BrowserFetcher`
  stub (Playwright container joins compose later — the app image stays slim).
- `config.needs_browser` and `config.proxy_pool` fields exist from day one; `polite_http`
  reads an optional proxy URL per pool name from env.
- robots.txt check helper in `polite_http`, unused by the API fetchers, ready for scrape mode.
- Per-source politeness overrides (scraping wants slower pacing, lower concurrency).

## 6. Adapters

- `Adapter.map(RawPosting) -> JobPosting | None` (None = unparseable item; counted + logged).
- Registry resolves `source.kind` → adapter; `config.adapter_override` wins if set.
- Per-ATS adapters map their known field names, strip HTML from descriptions
  (`selectolax`), parse dates, build canonical URLs.
- **Fallback adapter** (used for unknown kinds and as safety net): heuristic key search over the
  raw payload — title from first of `title|name|position|jobTitle`, url from
  `url|absolute_url|applyUrl|hostedUrl|link`, location from `location(.name)|offices[0]`, etc.;
  emits a JobPosting as long as it finds a title + url, else None. Unit-tested against real
  captured payloads from each ATS with the kind deliberately mislabeled.

## 7. Pipeline (one run)

```
tick → healthchecks /start ping
  → create runs row (status=running)
  → load enabled sources; fetch all concurrently under polite_http caps
  → per source: adapt → filter (compiled DB rules) → record run_source_results
  → dedup: upsert seen_jobs (bump last_seen_at for known; insert new)
  → new+matched → insert candidates (send_status=pending, raw payload attached)
  → drain outbox: send all pending candidates (incl. leftovers from crashed runs) to Discord
  → finalize runs row (status: success | partial (some sources failed) | failed)
  → healthchecks success ping (or /fail on crash)
```

- Per-source errors are contained: one source failing → `partial`, everything else proceeds.
- Overlap guard: if a run is still going when the next tick fires, skip and warn (30 min is
  generous; a skip means something is wrong → Sentry warning).
- Startup recovery: `runs` stuck in `running` are marked `failed` on boot.

## 8. Discord delivery

- Sender drains the `candidates` outbox serially, pacing ~1 msg/1.3 s — safely under the
  **5 msgs / 5 s per-channel bucket** (global bot limit is 50 req/s, irrelevant here).
- Reads `X-RateLimit-Remaining` / `X-RateLimit-Reset-After` and sleeps when the bucket empties;
  on 429 honors `retry_after` from the response body. Marks `sent`/`failed` (+attempts) per row —
  a crash mid-drain resumes next run with no loss and no dupes.
- Embed per job: title (link), company, location, posted date, source kind, truncated to
  Discord's limits (embed ≤ 6000 chars total, description ≤ 4096).
- Bot setup: create app + bot token, invite with `Send Messages`+`Embed Links` to your server;
  channel id via env.

## 9. Observability

- **structlog** JSON logs to stdout with run_id/source binding; docker `json-file` driver with
  `max-size: 10m, max-file: 3`.
- **Sentry**: init in entrypoint; per-source fetch/adapt exceptions captured with source context
  (grouped, so one bad board = one issue, not 48 emails/day); run-level crashes captured; circuit
  breaker trips send explicit events.
- **healthchecks.io**: one check, period 30 min, grace ~15 min. `/start` on tick,
  success ping on completion, `/fail` on crash. Covers app hangs, container death, **and the VPS
  dying** — anything that stops pings alerts you. Hetzner's built-in email alerts (CPU/disk) are
  enabled as a bonus; no further VPS observability needed.

## 10. Deployment (Hetzner)

- CX22, Ubuntu 24.04, non-root user + ssh keys, ufw (ssh only), unattended-upgrades, Docker CE.
- `docker-compose.yml`: `app` (built multi-stage: uv sync in builder → slim runtime, non-root)
  and `postgres:17-alpine` (named volume, not exposed publicly, healthcheck gating app start).
  `restart: unless-stopped` on both.
- Deploy = `git pull && docker compose up -d --build` (a `deploy.sh`; CI image registry is
  overkill for one box, easy to add later).
- Nightly `pg_dump` cron to a compose sidecar volume (dedup history is the only data you'd miss).
- `.env` on the box only: `DATABASE_URL`, `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`,
  `SENTRY_DSN`, `HEALTHCHECKS_URL`, optional `PROXY_POOL_*`.

## 11. Milestones

1. **Scaffold** — `uv init`, pyproject (ruff strict-ish, pyright strict, pytest), package layout,
   `config.py`, structlog setup, typer CLI shell. CI (GitHub Actions: ruff+pyright+pytest) if repo
   goes to GitHub.
2. **DB layer** — ORM models, Alembic init + first migration, repo functions, seed command
   (`jobtrack sources add greenhouse stripe --board-token stripe`, `jobtrack rules seed`).
3. **Fetch + adapt** — `polite_http`, Fetcher protocol, Greenhouse/Lever/Ashby fetchers +
   adapters, fallback adapter, fixtures from real API captures, respx tests.
4. **Pipeline** — filtering, dedup, run recording, `run-once` end-to-end against real APIs
   (dry-run flag prints matches instead of sending).
5. **Discord sender** — outbox drain, rate-limit handling, embed builder; live test to a channel.
6. **Scheduler + observability** — 30-min loop, overlap guard, Sentry, healthchecks pings,
   startup recovery.
7. **Docker + deploy** — Dockerfile, compose, deploy.sh, provision Hetzner box, go live.
8. **Stretch** — SmartRecruiters/Workable adapters, `scrape/` BrowserFetcher implementation.

## 12. Assumptions (flag if wrong)

- Dedup history is kept indefinitely (`last_seen_at` enables expiry later if wanted).
- One Discord channel for matches; season/CS criteria seeded as in §4 and tuned via CLI, not
  hardcoded.
- No web UI/admin — the typer CLI (run over `docker compose exec`) is the management surface.
- Source list starts with a hand-picked set of company board slugs you provide (plus I can seed a
  starter list of well-known tech companies on Greenhouse/Lever/Ashby).
