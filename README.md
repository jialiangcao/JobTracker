# jobtrack

Self-hosted job-listing watcher. Every 30 minutes it polls job sources (ATS public APIs:
Greenhouse, Lever, Ashby, SmartRecruiters, Workable), normalizes listings via per-source
adapters, filters for CS internships with DB-configurable regex rules, dedups against
history, and posts new matches as Discord embeds. Per-run results land in Postgres;
Sentry captures errors and healthchecks.io provides a dead-man liveness alert.

See `implementation_plan.md` for architecture details.

## Local development

```sh
uv sync                                  # install deps (Python 3.13)
docker run -d --name jobtrack-pg -e POSTGRES_USER=jobtrack -e POSTGRES_PASSWORD=jobtrack \
  -e POSTGRES_DB=jobtrack -p 5432:5432 postgres:17-alpine   # or any local Postgres
cp .env.example .env                     # fill in at least DATABASE_URL

uv run jobtrack db-upgrade               # apply migrations
uv run jobtrack rules seed               # default CS-internship filter rules
uv run jobtrack sources sync             # apply sources.toml

uv run jobtrack run-once --dry-run       # fetch + filter, print would-be matches, no writes
uv run jobtrack run-once                 # real run (sends to Discord if configured)
uv run jobtrack serve                    # the 30-minute loop
```

Checks:

```sh
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run pytest
```

## CLI

| Command | Purpose |
|---|---|
| `jobtrack serve` | Polling loop (what the container runs) |
| `jobtrack run-once [--dry-run]` | Single run; dry-run writes no dedup state and sends nothing |
| `jobtrack db-upgrade` | Apply Alembic migrations |
| `jobtrack sources sync [path] [--prune]` | Apply `sources.toml` (the normal way to manage sources) |
| `jobtrack sources add/list/enable/disable` | One-off source edits (`--slug` for ATS boards, `--url` for scrape) |
| `jobtrack rules seed/add/list/enable/disable` | Manage regex filter rules |

Filter semantics: rules are grouped by name prefix (`season:*` is one group); a job must
match every include group (rules within a group are OR'd) and no exclude rule.

## Sources

`sources.toml` is the source of truth for which boards get polled; commit changes to it and
apply them with `jobtrack sources sync`.

```toml
[[source]]
kind = "greenhouse"     # greenhouse | lever | ashby | smartrecruiters | workable | scrape
name = "Stripe"         # (kind, name) identifies the source — renaming creates a new one
slug = "stripe"         # board token from the job-board URL; scrape kinds use url instead
```

Sync adds new entries, updates changed ones, and leaves anything already correct alone. It
merges into `sources.config` rather than replacing it, so the ETag/Last-Modified validators
the pipeline stores there survive. `enabled` is only applied when the file states it
explicitly, so a sync won't resurrect a source the circuit breaker disabled — set
`enabled = true` to re-enable deliberately. Sources present in the database but absent from
the file are reported, and disabled (never deleted) with `--prune`.

The file is bind-mounted read-only into the app container, so no rebuild is needed:
`docker compose run --rm app jobtrack sources sync`.

## Discord setup

1. <https://discord.com/developers/applications> → New Application → Bot → copy token.
2. OAuth2 URL generator: scope `bot`, permissions **Send Messages** + **Embed Links**;
   invite the bot to your server.
3. Enable Developer Mode in Discord, right-click the target channel → Copy Channel ID.
4. Set `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` in `.env`.

## Alerting setup

- **Sentry**: create a Python project, put its DSN in `SENTRY_DSN`. Fetch/run errors and
  circuit-breaker trips show up grouped per issue.
- **healthchecks.io**: create a check with period 30 min, grace 15 min; put the ping URL
  in `HEALTHCHECKS_URL`. If the loop, container, or VPS dies silently, you get alerted.

## Deploying (Hetzner)

One-time provisioning (CX22, Ubuntu 24.04):

```sh
adduser deploy && usermod -aG sudo deploy         # then install your ssh key
ufw allow OpenSSH && ufw enable
apt-get update && apt-get install -y unattended-upgrades git
curl -fsSL https://get.docker.com | sh && usermod -aG docker deploy

su - deploy
git clone <this repo> jobtrack && cd jobtrack
cp .env.example .env && $EDITOR .env              # set POSTGRES_PASSWORD, tokens, DSNs
docker compose up -d --build
docker compose exec app jobtrack rules seed
docker compose exec app jobtrack sources sync
```

Subsequent deploys: `./deploy.sh`, then `docker compose exec app jobtrack sources sync` if
`sources.toml` changed.

Nightly DB backup (host crontab, `crontab -e`):

```cron
15 4 * * * cd ~/jobtrack && docker compose exec -T db sh -c 'pg_dump -U jobtrack jobtrack | gzip > /backups/jobtrack-$(date +\%a).sql.gz'
```

(Keeps 7 rotating daily dumps in the `pgbackups` volume.)

## Operations notes

- A source that fails 5 consecutive runs is auto-disabled (circuit breaker) with a Sentry
  event; re-enable after fixing with `jobtrack sources enable <id>`.
- Unsent matches persist in the `candidates` outbox and are retried next run — a crash or
  Discord outage never drops or duplicates a notification.
- `sources.config.politeness` can slow down individual sources, e.g.
  `{"politeness": {"max_concurrency": 1, "min_delay_ms": 3000, "max_delay_ms": 6000}}`.
