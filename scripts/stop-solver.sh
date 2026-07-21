#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if docker compose -f docker-compose.solver.yml ps -q 2>/dev/null | grep -q .; then
  docker compose -f docker-compose.solver.yml down
fi
if [[ -f turnstile-solver/stop.sh ]]; then
  bash turnstile-solver/stop.sh 2>/dev/null || true
fi
echo "[solver] stopped"
