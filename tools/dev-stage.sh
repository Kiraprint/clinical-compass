#!/usr/bin/env bash
# dev-stage: постоянный тестовый контур для интеграционных прогонов.
#
# Зачем: гонять api-тесты и Playwright в стеке, максимально похожем на прод
# (postgres + qdrant + redis + api + ui + reminders-worker + kb-enrichment),
# но СВОЁМ: свои контейнеры, свои тома, свои порты. Туннеля на стенд нет
# (ui слушает только 127.0.0.1), рабочая БД/коллекция не используются никогда.
#
# Оптимизация, ради которой стенд постоянный:
#   * контейнеры и тома живут между прогонами — Postgres и Qdrant не поднимаются
#     заново, демо-данные и индекс КБ сохраняются;
#   * образы пересобираются только когда менялся код (отпечаток в .stage/images.sha);
#   * демо-сид и индексация КБ выполняются только когда менялись их входы
#     (отпечаток в .stage/kb.sha) или коллекция пуста.
#
# Использование:
#   tools/dev-stage.sh up            # поднять/обновить стенд (по отпечаткам)
#   tools/dev-stage.sh up --data     # только postgres/qdrant/redis (для api-тестов)
#   tools/dev-stage.sh seed          # принудительно демо-сид + индексация КБ
#   tools/dev-stage.sh test          # интеграционные тесты в стенде (api + e2e)
#   tools/dev-stage.sh test --api    # только api-тесты
#   tools/dev-stage.sh test --e2e    # только Playwright
#   tools/dev-stage.sh status        # состояние, точки в КБ, отпечатки
#   tools/dev-stage.sh down          # остановить, данные сохранить
#   tools/dev-stage.sh reset         # снести вместе с данными
#
# Переменные: CC_STAGE_PROJECT (проект compose), CC_STAGE_PG_PORT (55432),
# CC_STAGE_QDRANT_PORT (56333), CC_STAGE_UI_PORT (8097), CC_STAGE_DB (compass_test),
# CC_STAGE_TEST_COLLECTION (kb_test), CC_STAGE_COLLECTION (kb_stage), CC_PY.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${CC_PY:-$ROOT/.venv/bin/python}"
# В worktree своего .venv нет — берём venv основного чекаута, но код под тестом
# обязан быть из этого дерева: PYTHONPATH перекрывает editable-установку app/compass.
# sed читает весь поток: awk с `exit` роняет git по SIGPIPE, и под pipefail
# подстановка возвращала 141, а `set -e` молча обрывал скрипт.
if [ ! -x "$PY" ]; then
  MAIN_WT="$(git worktree list --porcelain | sed -n '1s/^worktree //p')"
  if [ -n "$MAIN_WT" ] && [ -x "$MAIN_WT/.venv/bin/python" ]; then
    PY="$MAIN_WT/.venv/bin/python"
  fi
fi

STAGE_PROJECT="${CC_STAGE_PROJECT:-cc-dev-stage}"
PG_PORT="${CC_STAGE_PG_PORT:-55432}"
QDRANT_PORT="${CC_STAGE_QDRANT_PORT:-56333}"
UI_PORT="${CC_STAGE_UI_PORT:-8097}"
TEST_DB_NAME="${CC_STAGE_DB:-compass_test}"
TEST_COLLECTION="${CC_STAGE_TEST_COLLECTION:-kb_test}"
STACK_COLLECTION="${CC_STAGE_COLLECTION:-kb_stage}"

STATE_DIR="$ROOT/.stage"
OVERRIDE="$STATE_DIR/override.yml"
IMAGES_MARK="$STATE_DIR/images.sha"
KB_MARK="$STATE_DIR/kb.sha"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31mОТКАЗ:\033[0m %s\n' "$*" >&2; exit 1; }

# --- предохранители: стенд не должен уметь дотянуться до рабочего контура ------
[ "$PG_PORT" != 5432 ] || fail "порт 5432 — рабочий Postgres, стенду нужен свой (CC_STAGE_PG_PORT)"
[ "$QDRANT_PORT" != 6333 ] || fail "порт 6333 — рабочий Qdrant, стенду нужен свой (CC_STAGE_QDRANT_PORT)"
[ "$STACK_COLLECTION" != kb ] || fail "коллекция kb — рабочая (CC_STAGE_COLLECTION)"
[ "$TEST_COLLECTION" != kb ] || fail "коллекция kb — рабочая (CC_STAGE_TEST_COLLECTION)"
[ "$TEST_DB_NAME" != compass ] || fail "база compass — рабочая (CC_STAGE_DB)"
# Имена уходят в DDL (CREATE DATABASE) и в имена коллекций — только безопасный
# алфавит, чтобы опечатка не превратилась в SQL.
for _name in "$TEST_DB_NAME" "$TEST_COLLECTION" "$STACK_COLLECTION"; do
  [[ "$_name" =~ ^[a-z0-9_]{1,48}$ ]] || fail "недопустимое имя стенда: '$_name' (только a-z0-9_)"
done
[[ "$PG_PORT" =~ ^[0-9]{2,5}$ ]] || fail "некорректный порт Postgres: $PG_PORT"
[[ "$QDRANT_PORT" =~ ^[0-9]{2,5}$ ]] || fail "некорректный порт Qdrant: $QDRANT_PORT"
[[ "$UI_PORT" =~ ^[0-9]{2,5}$ ]] || fail "некорректный порт UI: $UI_PORT"

# Проект compose тоже защищаем: `-p infra` — это РАБОЧИЙ проект, и `reset`
# снёс бы его тома вместе с базой, реестром КБ и коллекцией kb_stage/kb.
[[ "$STAGE_PROJECT" == cc-* ]] || fail "проект стенда должен начинаться с 'cc-' (сейчас '${STAGE_PROJECT}'): рабочий проект infra запрещён"

# Наследованные CC_* из шелла подменяют поведение стенда (эмбеддер, коллекция,
# cookie-secure). Чистим и задаём ровно те значения, что и в CI.
unset CC_COOKIE_SECURE CC_EMBEDDER CC_RERANKER CC_ML_URL CC_ML_TOKEN CC_ML_TIMEOUT \
      CC_DEPERSONALIZE_NER CC_ASSISTANT_LLM_URL CC_CHAT_INSIGHTS_ENABLED \
      CC_ALLOW_DEMO_SECRETS CC_MAIL_DEMO_CODES CC_QDRANT_COLLECTION CC_QDRANT_URL \
      CC_KB_CHUNK_CHARS CC_KB_CHUNK_OVERLAP CC_ENRICH_CHUNK_CHARS CC_ENRICH_CHUNK_OVERLAP
# FR-25/FR-41: адреса внешних интеграций тоже не наследуем — экспорт
# CC_REMD_BASE_URL/CC_EMIAS_BASE_URL с боевого стенда увёл бы egress мимо
# моков профиля emias, а гейт остался бы зелёным. unset перекрывает
# compose-дефолты (http://emias-mock:8090, http://remd-mock:8091) и
# демо-секреты; флаги ENABLED/ALLOW_INSECURE выставляем сами ниже.
unset CC_REMD_ENABLED CC_REMD_BASE_URL CC_REMD_CLIENT_ID CC_REMD_CLIENT_SECRET \
      CC_REMD_LPU_UID CC_REMD_CALLBACK_URL CC_REMD_CALLBACK_TOKEN \
      CC_REMD_CALLBACK_MAX_KB CC_REMD_ALLOW_INSECURE CC_REMD_TIMEOUT \
      CC_REMD_MAX_ATTEMPTS CC_REMD_MAX_RESPONSE_BYTES CC_REMD_RETRY_AFTER_CAP \
      CC_REMD_SENDING_STALE_SEC CC_REMD_PATIENT_SNILS CC_REMD_DOCUMENT_KIND \
      CC_REMD_ORG_NAME CC_REMD_DEPARTMENT_OID CC_REMD_DEPARTMENT_NAME
# Параметры самого мока (compose передаёт их в remd-mock): чужие значения дали
# бы 401 на сверке client_id/secret — тоже не наследуем.
unset CC_REMD_MOCK_CLIENT_ID CC_REMD_MOCK_CLIENT_SECRET CC_REMD_MOCK_LPU_UID \
      CC_REMD_MOCK_CALLBACK_URL CC_REMD_MOCK_CALLBACK_TOKEN CC_REMD_MOCK_TIMEOUT_S
# FR-25: то же для ЕМИАС-шлюза — 9 полей config.py и параметры самого мока.
unset CC_EMIAS_ENABLED CC_EMIAS_BASE_URL CC_EMIAS_CLIENT_ID \
      CC_EMIAS_CLIENT_SECRET CC_EMIAS_ALLOW_INSECURE CC_EMIAS_TIMEOUT \
      CC_EMIAS_MAX_ATTEMPTS CC_EMIAS_MAX_RESPONSE_BYTES CC_EMIAS_SNILS_PEPPER
unset CC_EMIAS_MOCK_LATENCY_MS CC_EMIAS_MOCK_RATE_LIMIT CC_EMIAS_MOCK_TIMEOUT_S
export CC_EMBEDDER=hash
export CC_QDRANT_COLLECTION="$STACK_COLLECTION"
export CC_KB_CHUNK_CHARS=1800 CC_KB_CHUNK_OVERLAP=120
export CC_ENRICH_CHUNK_CHARS=1800 CC_ENRICH_CHUNK_OVERLAP=120
export CC_ML_URL= CC_ML_TOKEN= CC_ML_TIMEOUT=5
export CC_DEPERSONALIZE_NER=off CC_RERANKER=off
export CC_ASSISTANT_LLM_URL= CC_CHAT_INSIGHTS_ENABLED=false
export CC_ALLOW_DEMO_SECRETS=true CC_COOKIE_SECURE=false
# FR-48: демо-код сброса пароля в ответе — на стенде явно включён (SMTP нет),
# иначе поведение зависело бы от шелла, а сброс молча отвечал бы 503.
export CC_MAIL_DEMO_CODES=true
# Внешние интеграции в стенде включены и указывают на моки профиля emias:
# без них e2e-спеки протокола приёма (docs/18, FR-39…FR-41) проверяют только
# выключенное состояние. Адреса (CC_EMIAS_BASE_URL/CC_REMD_*) НЕ переопределяем —
# берутся дефолты compose (`http://emias-mock:8090`, `http://remd-mock:8091`),
# поэтому унаследованный адрес наружу подставить нельзя.
# Эти флаги видит и pytest, поэтому api-тесты запускаются с `env -u` (см. cmd_test):
# их кейсы проверяют выключенное состояние интеграций.
export CC_EMIAS_ENABLED=true CC_EMIAS_ALLOW_INSECURE=true
export CC_REMD_ENABLED=true CC_REMD_ALLOW_INSECURE=true

compose() { docker compose -f "$ROOT/infra/docker-compose.yml" -f "$OVERRIDE" -p "$STAGE_PROJECT" "$@"; }


write_override() {
  mkdir -p "$STATE_DIR"
  cat > "$OVERRIDE" <<YAML
# Сгенерировано tools/dev-stage.sh — руками не править.
services:
  postgres:
    ports: !override ["127.0.0.1:${PG_PORT}:5432"]
  qdrant:
    ports: !override ["127.0.0.1:${QDRANT_PORT}:6333"]
  ui:
    ports: !override ["127.0.0.1:${UI_PORT}:80"]
YAML
}

wait_qdrant() {
  for _ in $(seq 1 60); do
    curl -sf "http://127.0.0.1:${QDRANT_PORT}/healthz" >/dev/null 2>&1 && return 0
    sleep 1
  done
  fail "Qdrant не отвечает на 127.0.0.1:$QDRANT_PORT"
}

wait_pg() {
  for _ in $(seq 1 60); do
    $PY -c "import psycopg; psycopg.connect('postgresql://compass:compass@127.0.0.1:${PG_PORT}/compass', connect_timeout=2)" \
      2>/dev/null && return 0
    sleep 1
  done
  fail "Postgres не принимает соединения на 127.0.0.1:$PG_PORT"
}

ensure_test_db() {
  $PY - "$TEST_DB_NAME" "$PG_PORT" <<'PY'
import sys, psycopg
name, port = sys.argv[1], sys.argv[2]
admin = f"postgresql://compass:compass@127.0.0.1:{port}/compass"
with psycopg.connect(admin, autocommit=True) as conn:
    if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
        conn.execute(f'CREATE DATABASE "{name}"')
        print(f"  создана БД {name} (для pytest)")
    else:
        print(f"  БД {name} уже есть")
PY
}

# Отпечаток входов. ВАЖНО: берётся содержимое, а не имена — `git status` даёт
# только список файлов, поэтому две разные правки одного файла давали бы один
# хеш и образы не пересобирались бы (гейт зеленел бы на старом коде).
#   ls-files -s      — блоб-хеши отслеживаемых файлов;
#   diff HEAD        — содержимое незакоммиченных правок;
#   hash-object      — содержимое новых untracked-файлов.
# HEAD в отпечаток не входит: коммит, трогающий только docs/, не должен
# заставлять пересобирать образы и переиндексировать КБ.
fingerprint() {
  {
    git ls-files -s "$@" 2>/dev/null
    git diff HEAD -- "$@" 2>/dev/null
    git ls-files -o --exclude-standard -z -- "$@" 2>/dev/null \
      | while IFS= read -r -d '' f; do git hash-object -- "$f"; done
  } | sha256sum | cut -d' ' -f1
}

images_fp() { fingerprint api core ui ml pyproject.toml uv.lock infra; }
kb_fp() {
  { echo "$CC_EMBEDDER|$CC_KB_CHUNK_CHARS|$CC_KB_CHUNK_OVERLAP|$STACK_COLLECTION"
    fingerprint api/app/kb_seed.py api/app/seed.py api/app/data api/app/enrichment core/compass
  } | sha256sum | cut -d' ' -f1
}

kb_points() {
  $PY - "$QDRANT_PORT" "$STACK_COLLECTION" <<'PY'
import json, sys, urllib.request
port, coll = sys.argv[1], sys.argv[2]
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/collections/{coll}", timeout=5) as r:
        print(json.load(r)["result"]["points_count"])
except Exception:
    print(0)
PY
}

wait_ui() {
  for _ in $(seq 1 120); do
    curl -sf "http://127.0.0.1:${UI_PORT}/" >/dev/null 2>&1 && return 0
    sleep 2
  done
  fail "UI не поднялся на 127.0.0.1:$UI_PORT"
}

# api прогоняет alembic на старте: ждём healthy по healthcheck compose
# (wget в python:3.12-slim нет, поэтому спрашиваем docker, а не контейнер).
wait_api_healthy() {
  local api_id="" api_health=""
  api_id="$(compose ps -q api 2>/dev/null || true)"
  for _ in $(seq 1 60); do
    api_health="$(docker inspect -f '{{.State.Health.Status}}' "$api_id" 2>/dev/null || true)"
    [ "$api_health" = healthy ] && return 0
    sleep 2
  done
  fail "api не стал healthy за 120 с (${api_health:-нет контейнера})"
}
wait_remd() {
  # Порт remd-mock не публикуется на хост: спрашиваем /health из api — тем же
  # путём, которым пойдёт передача СЭМД.
  for _ in $(seq 1 60); do
    if compose exec -T api /app/.venv/bin/python -c \
      'import urllib.request; urllib.request.urlopen("http://remd-mock:8091/health", timeout=2)' \
      >/dev/null 2>&1; then
      log "remd-mock отвечает на /health — egress РЭМД доступен"
      return 0
    fi
    sleep 2
  done
  fail "remd-mock не отвечает на /health (профиль emias)"
}

# --- up -----------------------------------------------------------------------
cmd_up() {
  local data_only=0
  for arg in "$@"; do
    case "$arg" in
      --data) data_only=1 ;;
      *) fail "неизвестный аргумент up: $arg" ;;
    esac
  done

  mkdir -p "$STATE_DIR"
  write_override
  log "стенд $STAGE_PROJECT: Postgres :$PG_PORT, Qdrant :$QDRANT_PORT, UI :$UI_PORT (только 127.0.0.1)"

  compose up -d postgres qdrant redis >/dev/null
  wait_pg
  wait_qdrant
  ensure_test_db
  export CC_QDRANT_URL="http://127.0.0.1:${QDRANT_PORT}"

  if [ "$data_only" = 1 ]; then
    log "данные стенда готовы (--data: без api/ui)"
    return 0
  fi

  # Моки внешних интеграций (ЕМИАС + РЭМД) профиля emias: api смотрит на них
  # по внутренним адресам, порты на хост не публикуются. Нужны только стеку —
  # для api-тестов (--data) не поднимаются.
  compose --profile emias up -d emias-mock remd-mock >/dev/null

  local want have
  want="$(images_fp)"
  have="$(cat "$IMAGES_MARK" 2>/dev/null || true)"
  if [ "$want" != "$have" ] || [ -z "$(compose images -q api 2>/dev/null)" ]; then
    log "код изменился (или образов нет) — собираю образы (кеш слоёв docker сохраняется)…"
    # emias в профиле сборки: remd-mock тоже собирается из api/Dockerfile —
    # без него правился бы только api, а мок оставался на старом коде, хотя
    # отпечаток images_fp уже записан как актуальный.
    compose --profile seed --profile emias build
    compose --profile emias up -d --force-recreate api ui reminders-worker kb-enrichment remd-mock >/dev/null
    echo "$want" > "$IMAGES_MARK"
  else
    log "код не менялся — образы не пересобираю, поднимаю как есть"
    compose --profile emias up -d api ui reminders-worker kb-enrichment remd-mock >/dev/null
  fi
  wait_api_healthy
  wait_remd
  wait_ui

  local kb_want kb_have points
  kb_want="$(kb_fp)"
  kb_have="$(cat "$KB_MARK" 2>/dev/null || true)"
  points="$(kb_points)"
  if [ "$kb_want" != "$kb_have" ] || [ "$points" = 0 ]; then
    log "индекс КБ устарел (отпечаток/пустая коллекция) — демо-сид + индексация (точки: $points)…"
    compose --profile seed run --rm --no-deps seed
    compose --profile seed run --rm --no-deps kb-seed
    echo "$kb_want" > "$KB_MARK"
  else
    log "индекс КБ актуален (точек: $points) — не пересчитываю"
  fi

  log "стенд готов: http://127.0.0.1:$UI_PORT"
}

# --- seed (принудительно) -----------------------------------------------------
cmd_seed() {
  compose up -d postgres qdrant redis api >/dev/null
  wait_pg
  wait_qdrant
  ensure_test_db
  # Сид пишет в схему, которую создаёт alembic в api: без ожидания — гонка.
  wait_api_healthy
  log "демо-сид (идемпотентный, без --reset)…"
  compose --profile seed run --rm --no-deps seed
  log "индексация КБ (upsert, без --reset)…"
  compose --profile seed run --rm --no-deps kb-seed
  kb_fp > "$KB_MARK"
  log "точки в $STACK_COLLECTION: $(kb_points)"
}

# --- test ---------------------------------------------------------------------
cmd_test() {
  local do_api=0 do_e2e=0 extra=()
  for arg in "$@"; do
    case "$arg" in
      --api) do_api=1 ;;
      --e2e) do_e2e=1 ;;
      *) extra+=("$arg") ;;
    esac
  done
  [ "$do_api" = 1 ] || [ "$do_e2e" = 1 ] || { do_api=1; do_e2e=1; }

  if [ "$do_e2e" = 1 ]; then cmd_up; else cmd_up --data; fi

  if [ "$do_api" = 1 ]; then
    # CC_COOKIE_SECURE стенда (false для e2e по http) не должен влиять на pytest:
    # с не-Secure cookie TestClient переиспользует сессию и ломает проверки 401.
    # Флаги моков (EMIAS/REMD) тоже снимаем: api-тесты проверяют ВЫКЛЮЧЕННОЕ
    # состояние интеграций, иначе падают кейсы test_auth/test_emias_api.
    ( cd "$ROOT" && \
      env -u CC_COOKIE_SECURE \
          -u CC_EMIAS_ENABLED -u CC_EMIAS_ALLOW_INSECURE -u CC_EMIAS_BASE_URL \
          -u CC_REMD_ENABLED -u CC_REMD_ALLOW_INSECURE -u CC_REMD_BASE_URL \
          -u CC_REMD_CALLBACK_URL \
      PYTHONPATH="$ROOT/api:$ROOT/core${PYTHONPATH:+:$PYTHONPATH}" \
      CC_TEST_DATABASE_URL="postgresql+psycopg://compass:compass@127.0.0.1:${PG_PORT}/${TEST_DB_NAME}" \
      CC_QDRANT_URL="http://127.0.0.1:${QDRANT_PORT}" \
      CC_QDRANT_COLLECTION="$TEST_COLLECTION" \
      CC_ALLOW_DEMO_SECRETS=true \
      $PY -m pytest api/tests -q "${extra[@]}" )
  fi

  if [ "$do_e2e" = 1 ]; then
    log "e2e в стенде: http://127.0.0.1:$UI_PORT"
    # Окна лимитера логинов живут в redis стенда и переживают прогоны: без
    # сброса накопленные 401 от предыдущего прогона съедают лимит на IP и
    # auth-rate-limit.spec падает не по делу. redis стенда — только лимитер.
    compose exec -T redis redis-cli FLUSHDB >/dev/null 2>&1 || \
      log "предупреждение: не удалось сбросить redis стенда"
    [ -d "$ROOT/e2e/node_modules" ] || ( cd "$ROOT/e2e" && npm ci ) 9>&-
    # Логи api пишем потоком весь прогон: `logs --tail=N` после падения
    # показывает только хвост и теряет запрос, который и сломал спек.
    # 9>&- обязателен: фоновый `compose logs -f` унаследовал бы fd блокировки
    # и держал .stage/lock после выхода скрипта.
    local api_log="/tmp/cc-dev-stage-api.log"
    local logs_pid=""
    : > "$api_log"
    compose logs --no-color -f api >> "$api_log" 2>&1 9>&- &
    logs_pid=$!
    trap '[ -n "${logs_pid:-}" ] && kill "$logs_pid" 2>/dev/null; exit 130' INT TERM
    local e2e_rc=0
    ( cd "$ROOT/e2e" && CC_E2E_URL="http://127.0.0.1:${UI_PORT}" npx playwright test "${extra[@]}" ) 9>&- || e2e_rc=$?
    kill "$logs_pid" 2>/dev/null || true
    wait "$logs_pid" 2>/dev/null || true
    trap - INT TERM
    if [ "$e2e_rc" != 0 ]; then
      fail "e2e упали (rc=$e2e_rc); логи api: $api_log"
    fi
  fi

  log "интеграционные тесты в стенде: зелёные"
}

# --- status / down / reset ----------------------------------------------------
cmd_status() {
  [ -f "$OVERRIDE" ] || { log "стенд не поднимался"; return 0; }
  compose ps
  printf '  точки в %s: %s\n' "$STACK_COLLECTION" "$(kb_points 2>/dev/null || echo '?')"
  printf '  отпечаток образов: %s (сейчас %s)\n' "$(cat "$IMAGES_MARK" 2>/dev/null || echo нет)" "$(images_fp)"
  printf '  отпечаток КБ: %s (сейчас %s)\n' "$(cat "$KB_MARK" 2>/dev/null || echo нет)" "$(kb_fp)"
}

cmd_down() {
  [ -f "$OVERRIDE" ] || return 0
  # --profile emias/seed: без них `down` оставляет контейнеры моков и
  # one-shot сервисов, и `compose up` потом считает проект живым.
  compose --profile seed --profile emias down
  log "стенд остановлен, тома сохранены (reset — чтобы снести данные)"
}

cmd_reset() {
  # `--profile "*"` — снести и моки, и gpu-контур, если он поднимался.
  [ -f "$OVERRIDE" ] && compose --profile "*" down -v || true
  rm -rf "$STATE_DIR"
  log "стенд снесён вместе с данными"
}

cmd="${1:-}"; shift || true

case "$cmd" in
  up|seed|test|down|reset)
    # Один стенд — один мутирующий прогон. Два параллельных `test` (или `up`
    # во время `test`) роняют друг другу compose: postgres пересоздаётся
    # посреди чужого pytest («the database system is shutting down»), схема
    # compass_test портится. `status`/`help` блокировку не берут и не висят.
    mkdir -p "$STATE_DIR"
    exec 9>"$STATE_DIR/lock"
    if ! flock -n 9; then
      fail "стенд занят другим прогоном (lock: $STATE_DIR/lock); дождитесь его окончания"
    fi
    ;;
esac

case "$cmd" in
  up) cmd_up "$@" ;;
  seed) cmd_seed ;;
  test) cmd_test "$@" ;;
  status) cmd_status ;;
  down) cmd_down ;;
  reset) cmd_reset ;;
  ""|-h|--help|help) sed -n '2,32p' "$0" ;;
  *) fail "неизвестная команда: $cmd (up|seed|test|status|down|reset)" ;;
esac
