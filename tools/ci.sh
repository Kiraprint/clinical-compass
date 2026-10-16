#!/usr/bin/env bash
# Локальный CI: тесты ядра + API (реальный Postgres в Docker) + сборка образов
# (+ опционально e2e-проход всего стека с --e2e). Запускать из корня репо.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
COMPOSE="docker compose -f infra/docker-compose.yml"
TEST_DB="${CC_TEST_DATABASE_URL:-postgresql+psycopg://compass:compass@127.0.0.1:5432/compass_test}"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

# --- Postgres для тестов -----------------------------------------------------
log "Запуск Postgres + Qdrant (compose services)…"
$COMPOSE up -d postgres qdrant
# ожидаем готовности
for _ in $(seq 1 30); do
  if $COMPOSE exec -T postgres pg_isready -U compass -d compass >/dev/null 2>&1; then break; fi
  sleep 1
done
for _ in $(seq 1 30); do
  if $COMPOSE exec -T qdrant bash -c 'exec 3<>/dev/tcp/127.0.0.1/6333 && printf "GET /healthz HTTP/1.0\r\n\r\n" >&3 && grep -q 200 <&3' >/dev/null 2>&1; then break; fi
  sleep 1
done

log "Создание test-БД '$TEST_DB' (если нет)…"
$COMPOSE exec -T postgres psql -U compass -d compass -tc \
  "SELECT 1 FROM pg_database WHERE datname='compass_test'" | grep -q 1 \
  || $COMPOSE exec -T postgres psql -U compass -d compass -c "CREATE DATABASE compass_test"

# --- Тесты ядра --------------------------------------------------------------
log "[1/3] Тесты ядра (core/tests, coverage ≥ 95%)…"
$PY -m pytest core/tests --cov=compass --cov-fail-under=95 -q

# --- Тесты API ---------------------------------------------------------------
log "[2/3] Тесты API (api/tests против реального Postgres)…"
CC_TEST_DATABASE_URL="$TEST_DB" $PY -m pytest api/tests -q

# --- Сборка docker-образов ---------------------------------------------------
log "[3/3] Сборка docker-образов (api + ui)…"
$COMPOSE build api ui

# --- Опционально e2e ---------------------------------------------------------
if [[ "${1:-}" == "--e2e" ]]; then
  log "e2e: подъём полного стека и Playwright…"
  $COMPOSE up -d --build
  (cd e2e && npx playwright test)
fi

log "CI: всё зелёное ✅"
