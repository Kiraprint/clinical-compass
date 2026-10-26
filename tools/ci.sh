#!/usr/bin/env bash
# Локальный CI (замена GitHub Actions: биллинг GH исчерпан, гоняем у себя).
# Ворота те же, что в .github/workflows/ci.yml:
#   ядро (coverage ≥ 95%) → API против реального Postgres → ML-сайдкар (stub)
#   → валидация compose и сборка образов → опционально e2e (Playwright).
#
# Интеграционные прогоны (api-тесты и e2e) идут в ПОСТОЯННОМ стенде
# tools/dev-stage.sh (compose-проект cc-dev-stage: Postgres, Qdrant, redis, api, ui),
# а не в одноразовом стеке: контейнеры, демо-данные и индекс КБ живут между
# прогонами, поэтому БД и КБ не пересоздаются и не переиндексируются каждый раз.
# Стенд слушает только 127.0.0.1, туннеля на него нет, рабочая БД (compass на
# :5432) и рабочая коллекция (kb) не используются.
#
# Использование (из любой директории):
#   tools/ci.sh                  # всё, кроме e2e
#   tools/ci.sh --e2e            # + полный e2e-проход в стенде
#   tools/ci.sh --skip-docker    # без шага compose config/build
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${CC_PY:-$ROOT/.venv/bin/python}"
# В worktree своего .venv нет — берём venv основного чекаута, но код под тестом
# обязан быть из этого дерева: PYTHONPATH перекрывает editable-установку app/compass.
if [ ! -x "$PY" ]; then
  # sed читает поток целиком: awk с `exit` роняет git по SIGPIPE, под pipefail
  # подстановка возвращает 141, и `set -e` молча обрывает гейт.
  MAIN_WT="$(git worktree list --porcelain | sed -n '1s/^worktree //p')"
  if [ -n "$MAIN_WT" ] && [ -x "$MAIN_WT/.venv/bin/python" ]; then
    PY="$MAIN_WT/.venv/bin/python"
  fi
fi
export PYTHONPATH="$ROOT/api:$ROOT/core${PYTHONPATH:+:$PYTHONPATH}"
STAGE="$ROOT/tools/dev-stage.sh"
COMPOSE="docker compose -f infra/docker-compose.yml"

WITH_E2E=0
WITH_DOCKER=1
for arg in "$@"; do
  case "$arg" in
    --e2e) WITH_E2E=1 ;;
    --skip-e2e) WITH_E2E=0 ;;
    --skip-docker) WITH_DOCKER=0 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "неизвестный аргумент: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31mОТКАЗ:\033[0m %s\n' "$*" >&2; exit 1; }

[ -x "$PY" ] || fail "нет $PY — собери окружение: uv sync --all-packages"
[ -x "$STAGE" ] || fail "нет $STAGE — стенд интеграционных тестов"

# Предохранители от рабочего контура живут в dev-stage.sh (порты 5432/6333,
# имена compass/kb запрещены буквально), здесь дублируем только то, что
# проверяется до запуска контейнеров.
[ "${CC_STAGE_PG_PORT:-55432}" != 5432 ] || fail "порт 5432 — рабочий Postgres"
[ "${CC_STAGE_QDRANT_PORT:-56333}" != 6333 ] || fail "порт 6333 — рабочий Qdrant"

# --- 1. Ядро -----------------------------------------------------------------
log "[1/5] Ядро (core/tests, coverage ≥ 95%)…"
$PY -m pytest core/tests --cov=compass --cov-fail-under=95 -q

# --- 2. API ------------------------------------------------------------------
log "[2/5] API (api/tests в стенде: Postgres и Qdrant стенда)…"
"$STAGE" test --api

# --- 3. ML-сайдкар (stub, без GPU и без torch) --------------------------------
log "[3/5] ML-сайдкар (ml/tests, CC_ML_STUB)…"
if [ -d ml/tests ]; then
  ( cd ml && CC_ML_STUB=1 PYTHONPATH="$ROOT/ml:$ROOT/core" $PY -m pytest tests -q ) \
    || fail "ml/tests упали"
else
  log "ml/tests нет — пропуск"
fi

# --- 4. Валидация compose и сборка образов ------------------------------------
if [ "$WITH_DOCKER" = 1 ]; then
  log "[4/5] Docker: compose config (все профили) + сборка api/ui…"
  for p in "" "--profile seed" "--profile gpu" "--profile emias"; do
    $COMPOSE $p config -q || fail "compose config сломан ($p)"
  done
  $COMPOSE build api ui
else
  log "[4/5] Docker: пропущено (--skip-docker)"
fi

# --- 5. e2e в стенде ---------------------------------------------------------
if [ "$WITH_E2E" = 1 ]; then
  log "[5/5] e2e: Playwright против стенда (http://127.0.0.1:${CC_STAGE_UI_PORT:-8097})…"
  "$STAGE" test --e2e
else
  log "[5/5] e2e: пропущено (добавьте --e2e)"
fi

log "CI: всё зелёное"
