#!/usr/bin/env bash
# Start local Turnstile Solver (YesCaptcha-compatible API on 127.0.0.1:5072).
#
# Usage:
#   ./scripts/start-solver.sh              # 默认：本机进程（无需 Docker）
#   ./scripts/start-solver.sh --local      # 强制本机进程
#   ./scripts/start-solver.sh --docker     # Docker Compose
#   SOLVER_MODE=docker ./scripts/start-solver.sh
#
# Env (optional):
#   TURNSTILE_HOST / TURNSTILE_PORT / TURNSTILE_THREAD / TURNSTILE_BROWSER_TYPE
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

MODE="${SOLVER_MODE:-local}"
for arg in "$@"; do
  case "$arg" in
    -h|--help)
      cat <<'HELP'
Usage: ./scripts/start-solver.sh [--local|--docker] [-h|--help]

  --local   本机 Python 进程启动 turnstile-solver（默认，无需 Docker）
  --docker  使用 docker compose -f docker-compose.solver.yml 启动
  -h        显示帮助

环境变量:
  SOLVER_MODE=local|docker   默认启动方式（可被参数覆盖）
  TURNSTILE_THREAD=1         浏览器线程数（小机器建议 1）
  TURNSTILE_BROWSER_TYPE=camoufox
  TURNSTILE_PORT=5072
HELP
      exit 0
      ;;
    --local|-l|local)
      MODE=local
      ;;
    --docker|-d|docker)
      MODE=docker
      ;;
    *)
      echo "[solver] unknown argument: $arg (try --help)" >&2
      exit 2
      ;;
  esac
done

wait_ready() {
  local port="${TURNSTILE_PORT:-5072}"
  local max="${1:-90}"
  echo "[solver] waiting for http://127.0.0.1:${port}/ ..."
  for _ in $(seq 1 "$max"); do
    if curl -fsS -m 2 "http://127.0.0.1:${port}/" >/dev/null 2>&1; then
      echo "[solver] ready http://127.0.0.1:${port}"
      return 0
    fi
    sleep 2
  done
  return 1
}

start_local() {
  echo "[solver] mode=local (host process, no Docker)"
  if [[ ! -d "$ROOT/turnstile-solver" ]]; then
    echo "[solver] turnstile-solver/ not found under $ROOT" >&2
    exit 1
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "[solver] python3 not found; install Python 3.10+ or use --docker" >&2
    exit 1
  fi
  if ! command -v curl >/dev/null 2>&1; then
    echo "[solver] curl not found (needed for health check)" >&2
    exit 1
  fi
  cd "$ROOT/turnstile-solver"
  bash ./start.sh
}

start_docker() {
  echo "[solver] mode=docker (compose on 127.0.0.1:5072)"
  if ! command -v docker >/dev/null 2>&1; then
    echo "[solver] docker not found; fall back to --local" >&2
    start_local
    return
  fi
  if ! docker compose version >/dev/null 2>&1 && ! docker-compose version >/dev/null 2>&1; then
    echo "[solver] docker compose not available; fall back to --local" >&2
    start_local
    return
  fi
  docker compose -f docker-compose.solver.yml up -d --build
  if wait_ready 90; then
    exit 0
  fi
  echo "[solver] not ready yet; logs:" >&2
  docker compose -f docker-compose.solver.yml logs --tail=80 >&2 || true
  exit 1
}

case "$MODE" in
  local|host|native)
    start_local
    ;;
  docker|compose)
    start_docker
    ;;
  *)
    echo "[solver] invalid MODE=$MODE (use local|docker)" >&2
    exit 2
    ;;
esac
