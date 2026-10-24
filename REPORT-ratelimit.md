# Rate limiter логинов: общий счётчик в Redis

Ветка: `fix/ratelimit-shared` (worktree `.worktrees/ratelimit`), дата: 2026-10-24.

## Что изменено

| Файл | Изменение |
|---|---|
| `api/app/ratelimit.py` | Два хранилища: memory (как было) и Redis. Redis-режим — фиксированное окно: ключ `cc:rl:{pair\|ip}:{номер окна}:{sha256(идентификатора)[:32]}`, запись `INCR`+`EXPIRE` (TTL = 2 окна) в одном pipeline, проверка — `GET` обоих счётчиков. Ключи без открытых ПДн. `_resolve_backend()` фиксирует режим (PING) на старте; при ошибке Redis в рантайме — fail-open в memory, один WARNING и счётчик `redis_errors`. `reset_rate_limits()` чистит активное хранилище (Redis — `SCAN`+`DEL` по namespace) и сбрасывает выбор бэкенда. |
| `api/app/config.py` | `CC_RATE_BACKEND=auto\|memory\|redis` (auto), `CC_REDIS_URL` (пусто → memory), `CC_RATE_REDIS_TIMEOUT=0.3` (socket/connect). |
| `api/app/main.py` | Lifespan: `await asyncio.to_thread(ratelimit.init_backend)` — режим зафиксирован до первого логина, мёртвый Redis не роняет старт. |
| `api/pyproject.toml` + `uv.lock` | Опциональный extra `api[redis] = ["redis>=5"]` (в lock — redis 8.1.0); импорт redis-py внутри `_connect`, без extra модуль работает. |
| `api/Dockerfile` | `uv sync ... --extra redis`: redis-py есть в образе, но не является обязательной зависимостью проекта. |
| `infra/docker-compose.yml` | Сервис `redis` (redis:7-alpine, `restart: unless-stopped`, healthcheck `redis-cli ping`, порт не публикуется; persistence выключен — счётчики эфемерные) + `CC_REDIS_URL`/`CC_RATE_BACKEND` у `api` + мягкий `depends_on` (`service_started`, недоступный Redis не блокирует старт). |
| `infra/.env.example` | `CC_REDIS_URL`, `CC_RATE_BACKEND`, `CC_RATE_REDIS_TIMEOUT` с пояснениями single-node/multi-replica. |
| `api/tests/conftest.py` | `CC_RATE_BACKEND=memory` для всех тестов (не зависят от живого Redis). |
| `api/tests/test_ratelimit_redis.py` | 15 тестов на фейковом Redis (общий бюджет двух «процессов», атомарность INCR, TTL, fail-open без зависания, выбор бэкенда, отсутствие пакета redis, сброс). |
| Документация | `docs/03` §12 (bullet + ограничения), `docs/10` R24, `docs/02` §5 (инвентарь: хеши+числа, TTL), `docs/06` §1 (пирамида 544→559) и таблица тестов, README (раздел «Пул БД и число воркеров»). |

## Замер: бюджет общий, а не на процесс

Условия: PostgreSQL `127.0.0.1:5432`, два отдельных процесса uvicorn (по одному
воркеру; роль «двух реплик/процессов»), `CC_RATE_MAX=5`, `CC_RATE_WINDOW=60`,
`CC_RATE_IP_MAX=1000` (изолируем счётчик пары), один email, неверный пароль.

| Режим | Процессы | Последовательность статусов | 429 на попытке |
|---|---|---|---|
| `CC_REDIS_URL` задан (redis 7, `auto`) | 28081 + 28082 | `401 401 401 401 401 429 429` | **6-я** (суммарно 5) |
| memory (`CC_REDIS_URL` пуст) | 18083 + 18084 | `401 ×10 429 429` | **11-я** (5 + 5) |

Состояние Redis после redis-прогона (доказательство атомарного общего счёта и TTL):

```
cc:rl:pair:29881008:c1b0c78f9990ec204fbba1b84c06a96d  val=5  ttl=114
cc:rl:ip:29881008:12ca17b49af2289436f303e0166030a2    val=5  ttl=114
```

В ключах нет email/IP (только SHA-256), TTL ≤ 2×60 c, значение — суммарные
неудачи обоих процессов. Итог: старый множитель `N × CC_RATE_MAX` в redis-режиме
устранён; memory-режим остаётся задокументированным поведением для демо/одного
процесса.

## Воспроизведение

```bash
# 1) Redis и отдельные БД
docker run -d --rm --name cc-ratelimit-redis -p 127.0.0.1:26379:6379 redis:7-alpine
docker exec infra-postgres-1 psql -U compass -d postgres -c "CREATE DATABASE rl_measure_redis"
docker exec infra-postgres-1 psql -U compass -d postgres -c "CREATE DATABASE rl_measure_mem"

# 2) Два процесса с общим Redis и два без него (порты — любые свободные)
cd api
export PYTHONPATH=$PWD:/path/to/worktree/core CC_ALLOW_DEMO_SECRETS=true CC_PUSH_ENABLED=0 \
       CC_RATE_MAX=5 CC_RATE_WINDOW=60 CC_RATE_IP_MAX=1000
CC_DATABASE_URL=postgresql+psycopg://compass:compass@127.0.0.1:5432/rl_measure_redis \
CC_REDIS_URL=redis://127.0.0.1:26379/0 CC_RATE_BACKEND=auto \
  uvicorn app.main:app --host 127.0.0.1 --port 28081 &
# ... повторить для 28082; для memory-пары — без CC_REDIS_URL (БД rl_measure_mem, 18083/18084)

# 3) Регистрация email и 7/12 неудачных логинов вперемешку по портам
#    (см. /tmp/rl_measure.py из сессии: POST /api/v1/auth/register, затем
#     POST /api/v1/auth/login с неверным паролем, чередуя порты)
```

Тесты:

```bash
cd api
CC_ALLOW_DEMO_SECRETS=true \
CC_TEST_DATABASE_URL=postgresql+psycopg://compass:compass@127.0.0.1:5432/ratelimit_test \
CC_QDRANT_COLLECTION=kb_ratelimit_test \
PYTHONPATH=$PWD:../core ../.venv/bin/python -m pytest \
  tests/test_async_db.py tests/test_auth.py tests/test_ratelimit.py tests/test_ratelimit_redis.py -q
# 43 passed
```

`docker compose -f infra/docker-compose.yml --env-file infra/.env.example config`
— валиден (`exit=0`).

## Принятые ограничения

- Redis-окно **фиксированное**: на стыке двух окон возможен всплеск до
  `2 × CC_RATE_MAX`. В memory-режиме окно скользящее.
- При недоступном Redis / ошибке / отсутствии extra — **fail-open**: счётчик
  деградирует в memory, общий бюджет ослабляется до per-process; событие
  видно в WARNING и `ratelimit.redis_errors`. Логин не зависает дольше
  `CC_RATE_REDIS_TIMEOUT` (ретраи redis-py отключены: `Retry(NoBackoff(), 0)`).
- Redis в compose — single-node, без пароля и без persistence (порт не
  публикуется). Для HA/нескольких хостов нужен внешний Redis (Sentinel/кластер)
  и `CC_REDIS_URL` на него; после рестарта Redis окна начинаются заново.
- `check_rate`/`record_rate_hit` остаются синхронными вызовами в async-ручке
  (как было): при живом Redis это доли миллисекунды, худший случай ограничен
  socket_timeout.
- Тесты не требуют Redis: фейковый клиент моделирует общее хранилище и TTL;
  тест конфигурации клиента пропускается, если пакет redis не установлен.
