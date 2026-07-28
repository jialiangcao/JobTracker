#!/usr/bin/env bash
# Deploy on the VPS: pull, rebuild, restart, prune old images.
set -euo pipefail
cd "$(dirname "$0")"

git pull --ff-only
docker compose build
docker compose up -d
docker image prune -f
docker compose ps
