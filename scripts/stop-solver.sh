#!/usr/bin/env bash
# Stop Turnstile Solver (Docker compose and/or host process).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

stopped=0
if command -v docker >/dev/null 2>&1; then
  if docker compose -f docker-compose.solver.yml ps -q 2>/dev/null | grep -q .; then
    docker compose -f docker-compose.solver.yml down
    stopped=1
  fi
fi
if [[ -f turnstile-solver/stop.sh ]]; then
  bash turnstile-solver/stop.sh 2>/dev/null && stopped=1 || true
fi
if [[ "$stopped" -eq 1 ]]; then
  echo "[solver] stopped"
else
  echo "[solver] nothing running (or already stopped)"
fi
