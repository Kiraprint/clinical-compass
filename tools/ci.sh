#!/usr/bin/env bash
# Локальный CI (замена GitHub Actions: биллинг GH исчерпан, гоняем у себя).
# Проверяет ровно те же ворота, что .github/workflows/ci.yml:
#   ядро (coverage ≥ 95%) → API против реального Postgres → ML-сайдкар (stub)
#   → сборка docker-образов api/ui → опционально e2e (Playwright, полный стек).
#
# Использование (из любой директории):
#   tools/ci.sh                  # всё, кроме e2e
#   tools/ci.sh --e2e            # + полный e2e-проход
#   tools/ci.sh --skip-docker    # без docker build
#
# Изоляция: тестовая БД по умолчанию «compass_ci» и коллекция Qdrant «kb_ci»,
# чтобы не пересекаться с чужими прогонами (compass_test/kb_test). Свои адреса
# можно передать через CC_TEST_DATABASE_URL / CC_QDRANT_COLLECTION.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${CC_PY:-$ROOT/.venv/bin/python}"
# В worktree своего .venv нет — берём venv основного чекаута, но код под тестом
# обязан быть из этого дерева: PYTHONPATH перекрывает editable-установку app/compass.
if [ ! -x "$PY" ]; then
  MAIN_WT="$(git worktree list --porcelain | awk '/^worktree /{print $2; exit}')"
  [ -n "$MAIN_WT" ] && [ -x "$MAIN_WT/.venv/bin/python" ] && PY="$MAIN_WT/.venv/bin/python"
fi
export PYTHONPATH="$ROOT/api:$ROOT/core${PYTHONPATH:+:$PYTHONPATH}"
COMPOSE="docker compose -f infra/docker-compose.yml"
DEV_COMPOSE="docker compose -f infra/docker-compose.yml -f infra/docker-compose.dev.yml"
TEST_DB_NAME="${CC_CI_DB_NAME:-compass_ci}"
TEST_DB="${CC_TEST_DATABASE_URL:-postgresql+psycopg://compass:compass@127.0.0.1:5432/${TEST_DB_NAME}}"
CI_COLLECTION="${CC_CI_COLLECTION:-kb_ci}"
E2E_PROJECT="cci"
E2E_PORT="${CC_CI_E2E_PORT:-8096}"
E2E_OVERRIDE=""

WITH_E2E=0
WITH_DOCKER=1
for arg in "$@"; do
  case "$arg" in
    --e2e) WITH_E2E=1 ;;
    --skip-e2e) WITH_E2E=0 ;;
    --skip-docker) WITH_DOCKER=0 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "неизвестный аргумент: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31mОТКАЗ:\033[0m %s\n' "$*" >&2; exit 1; }

[ -x "$PY" ] || fail "нет $PY — собери окружение: uv sync --all-packages"

# Защита от прогона против «настоящей» БД: фикстура api/tests/conftest.py дропает схему.
case "$TEST_DB" in
  *"/compass"|*"/compass_demo"|*"/compass_alembic")
    fail "тестовая БД '$TEST_DB' похожа на рабочую — задайте CC_CI_DB_NAME" ;;
esac

# Унаследованные CC_* меняют поведение тестов (cookie-secure ломает ожидания
# 401, эмбеддер/коллекция подменяют индекс) — чистим и задаём свои.
unset CC_COOKIE_SECURE CC_EMBEDDER CC_RERANKER CC_ML_URL CC_ML_TOKEN \
      CC_DEPERSONALIZE_NER CC_ASSISTANT_LLM_URL CC_CHAT_INSIGHTS_ENABLED \
      CC_ALLOW_DEMO_SECRETS CC_QDRANT_COLLECTION
export CC_ALLOW_DEMO_SECRETS=true
export CC_QDRANT_COLLECTION="$CI_COLLECTION"

# --- 1. Postgres + Qdrant ----------------------------------------------------
# Если dev-порты уже опубликованы (обычно infra с docker-compose.dev.yml), берём
# их: второй проект в этой же рабочей папке конфликтовал бы на 5432/6333.
port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
if port_open 5432 && port_open 6333; then
  log "использую уже опубликованные Postgres :5432 и Qdrant :6333"
else
  log "поднимаю Postgres + Qdrant (compose services)…"
  $DEV_COMPOSE up -d postgres qdrant >/dev/null
  for _ in $(seq 1 40); do port_open 5432 && port_open 6333 && break; sleep 1; done
  port_open 5432 || fail "Postgres не поднялся на 127.0.0.1:5432"
  port_open 6333 || fail "Qdrant не поднялся на 127.0.0.1:6333"
fi
curl -sf http://127.0.0.1:6333/healthz >/dev/null || fail "Qdrant не отвечает на 127.0.0.1:6333"

log "БД для тестов: $TEST_DB_NAME"
$PY - "$TEST_DB_NAME" <<'PY'
import sys, psycopg
name = sys.argv[1]
admin = "postgresql://compass:compass@127.0.0.1:5432/compass"
with psycopg.connect(admin, autocommit=True) as conn:
    exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
    if not exists:
        conn.execute(f'CREATE DATABASE "{name}"')
        print(f"  создана БД {name}")
    else:
        print(f"  БД {name} уже есть")
PY

# --- 2. Ядро -----------------------------------------------------------------
log "[1/5] Ядро (core/tests, coverage ≥ 95%)…"
$PY -m pytest core/tests --cov=compass --cov-fail-under=95 -q

# --- 3. API ------------------------------------------------------------------
log "[2/5] API (api/tests против реального Postgres, коллекция $CI_COLLECTION)…"
CC_TEST_DATABASE_URL="$TEST_DB" $PY -m pytest api/tests -q

# --- 4. ML-сайдкар (stub, без GPU и без torch) --------------------------------
log "[3/5] ML-сайдкар (ml/tests, CC_ML_STUB)…"
if [ -d ml/tests ]; then
  ( cd ml && CC_ML_STUB=1 PYTHONPATH="$ROOT/ml:$ROOT/core" $PY -m pytest tests -q ) \
    || fail "ml/tests упали"
else
  log "ml/tests нет — пропуск"
fi

# --- 5. Сборка образов + валидация compose -----------------------------------
if [ "$WITH_DOCKER" = 1 ]; then
  log "[4/5] Docker: compose config (все профили) + сборка api/ui…"
  for p in "" "--profile seed" "--profile gpu" "--profile emias"; do
    $COMPOSE $p config -q || fail "compose config сломан ($p)"
  done
  $COMPOSE build api ui
else
  log "[4/5] Docker: пропущено (--skip-docker)"
fi

# --- 6. e2e ------------------------------------------------------------------
if [ "$WITH_E2E" = 1 ]; then
  log "[5/5] e2e: изолированный стек (проект $E2E_PROJECT, порт $E2E_PORT)…"
  E2E_OVERRIDE="$(mktemp -d)/override.yml"
  cat > "$E2E_OVERRIDE" <<YAML
services:
  ui:
    ports: !override ["127.0.0.1:${E2E_PORT}:80"]
YAML
  cleanup() {
    if [ "${CI_E2E_FAILED:-0}" = 1 ]; then
      # Красный e2e без логов api не диагностируется (стек сейчас снесём).
      docker compose -f infra/docker-compose.yml -f "$E2E_OVERRIDE" -p "$E2E_PROJECT" \
        logs --no-color --tail=300 api > /tmp/cc-ci-e2e-api.log 2>&1 || true
      log "логи api упавшего e2e: /tmp/cc-ci-e2e-api.log"
    fi
    CC_ALLOW_DEMO_SECRETS=true CC_COOKIE_SECURE=false \
      docker compose -f infra/docker-compose.yml -f "$E2E_OVERRIDE" -p "$E2E_PROJECT" \
      --profile seed down -v >/dev/null 2>&1 || true
    rm -rf "$(dirname "$E2E_OVERRIDE")"
  }
  trap cleanup EXIT
  # Стек e2e не должен тянуть прод-настройки из infra/.env: свой эмбеддер,
  # своя коллекция, без ML-сайдкара (иначе api ждёт http://ml:8080 по 30 с)
  # и без внешней LLM (ответы ассистента — retrieval-only, как в GH CI).
  CC_EMBEDDER=hash CC_QDRANT_COLLECTION="kb_${E2E_PROJECT}" CC_ML_URL= \
  CC_DEPERSONALIZE_NER=off CC_RERANKER=off CC_ASSISTANT_LLM_URL= \
  CC_CHAT_INSIGHTS_ENABLED=false \
  CC_ALLOW_DEMO_SECRETS=true CC_COOKIE_SECURE=false \
    docker compose -f infra/docker-compose.yml -f "$E2E_OVERRIDE" \
      -p "$E2E_PROJECT" --profile seed up -d --build
  for _ in $(seq 1 120); do
    curl -sf "http://127.0.0.1:${E2E_PORT}/" >/dev/null 2>&1 && break
    sleep 2
  done
  curl -sf "http://127.0.0.1:${E2E_PORT}/" >/dev/null || fail "UI не поднялся на :$E2E_PORT"
  # kb-seed — one-shot: UI поднимается раньше, чем коллекция создана, и ассистент
  # в первые секунды получил бы пустую базу. Ждём завершения индексатора.
  kb_state=""
  for _ in $(seq 1 120); do
    kb_id="$($COMPOSE -p "$E2E_PROJECT" --profile seed ps -aq kb-seed 2>/dev/null || true)"
    if [ -n "$kb_id" ]; then
      kb_state="$(docker inspect -f '{{.State.Status}}/{{.State.ExitCode}}' "$kb_id" 2>/dev/null || true)"
    fi
    [ "$kb_state" = "exited/0" ] && break
    sleep 1
  done
  [ "$kb_state" = "exited/0" ] || fail "kb-seed не завершился успешно: ${kb_state:-нет контейнера}"
  if [ ! -d e2e/node_modules ]; then
    ( cd e2e && npm ci )
  fi
  ( cd e2e && CC_E2E_URL="http://127.0.0.1:${E2E_PORT}" npx playwright test ) \
    || { CI_E2E_FAILED=1; fail "e2e упали"; }
else
  log "[5/5] e2e: пропущено (добавьте --e2e)"
fi

log "CI: всё зелёное"
