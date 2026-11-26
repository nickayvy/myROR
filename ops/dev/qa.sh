#!/usr/bin/env bash
# Bring up an isolated stack, migrate, run pytest + ruff inside the api container,
# tear down. Usage (all optional):
#   COMPOSE_PROJECT_NAME=myror-b PG_PORT=54322 MINIO_PORT=59002 API_PORT=8802 \
#     ops/dev/qa.sh [pytest args...]
#   KEEP=1 ops/dev/qa.sh     # leave the stack running afterwards
set -euo pipefail

unset ANTHROPIC_BASE_URL ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN FAL_KEY DATABASE_URL || true

cd "$(dirname "$0")"
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-myror-qa}"
export PG_PORT="${PG_PORT:-54329}"
export MINIO_PORT="${MINIO_PORT:-59009}"
export API_PORT="${API_PORT:-8809}"

dc() { docker compose -f docker-compose.yml "$@"; }

cleanup() {
  if [[ "${KEEP:-0}" != "1" ]]; then
    dc down -v --remove-orphans >/dev/null 2>&1 || true
  else
    echo "KEEP=1: stack $COMPOSE_PROJECT_NAME left running (api on 127.0.0.1:$API_PORT)"
  fi
}
trap cleanup EXIT

echo "== up ($COMPOSE_PROJECT_NAME: pg $PG_PORT, minio $MINIO_PORT, api $API_PORT)"
dc up -d --build --wait

echo "== migrate"
dc exec -T api python -m db.migrate

echo "== pytest"
status=0
dc exec -T api pytest -q -p no:cacheprovider "$@" || status=$?

echo "== ruff"
dc exec -T api ruff check --no-cache . || status=$?

exit $status
