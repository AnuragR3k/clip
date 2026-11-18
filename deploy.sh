#!/usr/bin/env bash
# Run on the VPS after a git pull. Keeps data volumes intact.
set -euo pipefail

cd "$(dirname "$0")"
docker compose build clipper
docker compose up -d clipper
docker compose --quiet logs --tail 20 clipper || true
