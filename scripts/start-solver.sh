#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if command -v docker >/dev/null 2>&1; then
  echo "[solver] starting via docker compose (turnstile-solver on 127.0.0.1:5072)..."
  docker compose -f docker-compose.solver.yml up -d --build
  echo "[solver] waiting for health..."
  for i in $(seq 1 90); do
    if curl -fsS -m 2 "http://127.0.0.1:5072/" >/dev/null 2>&1; then
      echo "[solver] ready http://127.0.0.1:5072"
      exit 0
    fi
    sleep 2
  done
  echo "[solver] not ready yet; logs:" >&2
  docker compose -f docker-compose.solver.yml logs --tail=80 >&2 || true
  exit 1
fi

echo "[solver] docker not found; falling back to host start.sh"
cd "$ROOT/turnstile-solver"
bash ./start.sh
