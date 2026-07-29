#!/usr/bin/env bash
# Exercise the containerized stack locally: build, migrate, seed, one pipeline run.
# Uses the compose `db` service and its own pgdata volume — the standalone
# jobtrack-pg dev container is untouched.
set -euo pipefail
cd "$(dirname "$0")"

MODE=dry
FRESH=0
DOWN=0

usage() {
  cat <<'EOF'
Usage: ./local-test.sh [--fresh] [--live | --serve]
       ./local-test.sh --down

  (default)  build, migrate, seed, then one dry run: no dedup writes, nothing sent
  --live     real run instead — writes dedup state and posts to Discord
  --serve    leave the stack running on the 30-minute loop and tail its logs
  --fresh    delete the compose database volume first, for a clean slate
  --down     stop the stack and exit
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --fresh) FRESH=1 ;;
    --live) MODE=live ;;
    --serve) MODE=serve ;;
    --down) DOWN=1 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is not running. Start Docker Desktop (open -a Docker) and retry." >&2
  exit 1
fi
if [ ! -f .env ]; then
  echo ".env is missing. Run: cp .env.example .env" >&2
  exit 1
fi

if [ "$DOWN" = 1 ]; then
  step "Stopping stack"
  docker compose down
  exit 0
fi

if [ "$FRESH" = 1 ]; then
  step "Removing existing stack and database volume"
  docker compose down -v
fi

step "Building app image"
docker compose build

step "Starting Postgres"
docker compose up -d db
cid=$(docker compose ps -q db)
for _ in $(seq 1 45); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$cid")" = healthy ] && break
  sleep 2
done
if [ "$(docker inspect -f '{{.State.Health.Status}}' "$cid")" != healthy ]; then
  echo "Postgres did not become healthy in 90s. Check: docker compose logs db" >&2
  exit 1
fi

step "Applying migrations"
docker compose run --rm -T app jobtrack db-upgrade

step "Seeding filter rules"
docker compose run --rm -T app jobtrack rules seed

step "Syncing sources.toml"
docker compose run --rm -T app jobtrack sources sync

if [ "$MODE" = serve ]; then
  step "Starting the polling loop (Ctrl-C stops tailing; stack keeps running)"
  docker compose up -d
  docker compose logs -f app
  exit 0
fi

if [ "$MODE" = live ]; then
  step "Live run — writes dedup state and sends to Discord"
  docker compose run --rm -T app jobtrack run-once
else
  step "Dry run — no dedup writes, nothing sent"
  docker compose run --rm -T app jobtrack run-once --dry-run
fi

step "Database state"
docker compose exec -T db psql -U jobtrack -d jobtrack -c \
  "SELECT (SELECT count(*) FROM sources) sources,
          (SELECT count(*) FROM filter_rules) rules,
          (SELECT count(*) FROM seen_jobs) seen,
          (SELECT count(*) FROM candidates WHERE send_status='pending') pending,
          (SELECT count(*) FROM candidates WHERE send_status='sent') sent;"
docker compose exec -T db psql -U jobtrack -d jobtrack -c \
  "SELECT source_id, status, http_status, fetched_count, matched_count, new_count, duration_ms
   FROM run_source_results WHERE run_id = (SELECT max(id) FROM runs) ORDER BY source_id;"

step "Done — stack still up. Stop it with: ./local-test.sh --down"
