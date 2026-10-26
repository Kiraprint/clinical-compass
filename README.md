# Клинический компас 2026 — цифровой инструмент диагностики и ведения тревоги/депрессии

Кейс-чемпионат: Авинейро (ГК «ХимРар») · НМИЦ ПН им. Сербского · Сеченовский Университет.
Демо-день: 30.10.2026. Рабочий язык: русский.

## Документация

| Документ | Содержание |
|---|---|
| [ТЗ](docs/ТЗ.md) | тех. задание по ГОСТ 19.201-78 + карта соответствия документов ГОСТ 19 (ЕСПД) |
| [01-бизнес-анализ](docs/01-бизнес-анализ.md) | контекст, стейкхолдеры, скоуп MVP, метрики успеха, риски |
| [02-системный-анализ](docs/02-системный-анализ.md) | акторы, сценарии, модель нагрузки, данные, ГОСТ/регламенты РФ |
| [03-архитектура](docs/03-архитектура.md) | слои, компоненты, стек, контракты, модель угроз |
| [04-спецификация](docs/04-спецификация.md) | FR/NFR, API-контракты, схемы данных (DDL) |
| [05-SDD](docs/05-SDD.md) | контракты ядра → эпики → реализация, трейсабельность |
| [06-TDD](docs/06-TDD.md) | тесты по контрактам, red-green, регрессия |
| [07-векторная-БД](docs/07-векторная-БД.md) | ADR: выбор векторной БД (Qdrant) для ИИ-агента врача, гибридный поиск |
| [08-база-знаний](docs/08-база-знаний.md) | дизайн базы знаний: состав корпуса, chunking, эмбеддинги bge-m3, пайплайн индексации |
| [10-модель-угроз](docs/10-модель-угроз.md) | STRIDE + реестр рисков R1-R12 (ГОСТ 14971) |
| [12-деперсонализация](docs/12-деперсонализация.md) | деперсонализация входа LLM (ФИО/адреса/документы/тел.) |
| [11-апробация-и-метрики](docs/11-апробация-и-метрики.md) | клинические сценарии, замеры метрик 1/4/6 |
| [09-дефициты-и-решения](docs/09-дефициты-и-решения.md) | сопоставление проекта с кейсом (9 условий, 8 задач, 13 метрик) + 13 дефицитов и дизайн решений; P1-P8 реализованы (Авиандр в KB, маршрутизация, НЯ, приверженность, парсинг историй, врач↔пациент чат, КР Минздрава) |
| [13-критерии-отбора-статей](docs/13-критерии-отбора-статей.md) | критерии отбора статей: свежесть, дизайн, релевантность, язык, ретракции; исполнение в коде (v0.2 — PubMed + ВОЗ) |
| [15-обогащение-базы-знаний](docs/15-обогащение-базы-знаний.md) | реестр `kb_works` как источник истины, Qdrant как индекс; источники, критерии, ревью, воспроизводимость, эксплуатация, ограничения |
| [17-локальная-модель](docs/17-локальная-модель.md) | локальная OpenAI-совместимая модель в контуре (llama.cpp в `infra/docker-compose.local-llm.yml`): запуск, переключение ассистента, замер качества на `bench/`, ограничения |
| [18-клинические-сессии](docs/18-клинические-сессии.md) | контракт FR-38…FR-41: календарь приёмов, сессия и протокол приёма (ручной + ИИ-черновик), УКЭП и PDF, передача в ЕМИАС; схема, API, аудит, шифрованные артефакты |
| [bench/](bench/README.md) | бенчмарк качества поиска: выбор эмбеддера для RU-EN психиатрии на реальных мета-анализах и гайдлайнах ВОЗ |

## Структура

```
core/   — клинический движок (SDK): шкалы, скоринг, триаж, риск-флаги, чек-ины, тренды (чистый Python)
api/    — FastAPI: auth/2FA, RBAC, consent (152-ФЗ), аудит (append-only), скрининг, эскалации,
          дневник, чат, панель врача, ростер, ИИ-агент (гибридный поиск по базе знаний),
          обогащение базы знаний (app/enrichment/: PubMed, ВОЗ IRIS, критерии, ревью), симулятор демо-данных
ui/     — React + Vite: пациент (скрининг/чек-ин/дневник/чат/тренд), панель врача
          (включая «Ассистент» и «Библиотеку препаратов»), админ-аудит и панель «База знаний»
infra/  — docker compose (postgres + qdrant + api + kb-seed + kb-enrichment + ui + seed)
e2e/    — Playwright: питч-сценарий (docs/04 §6), потоки пациента + врача
docs/   — ТЗ (ГОСТ 19.201-78) + 01–17 (разработочная документация), карта соответствия ГОСТ 19
```

## Запуск (демо / демо-день)

```bash
docker compose -f infra/docker-compose.yml up -d --build
# seed выполняется автоматически (12 пациентов × 14 дней)
# UI:   http://localhost:8090
# API:  http://localhost:8000 (OpenAPI: /docs)
```

**Демо-доступы** (создаёт симулятор):

| Роль | Логин | Пароль | 2FA (TOTP) |
|---|---|---|---|
| Админ | admin@compass.demo | Demo-Pass-1 | JBSWY3DPEHPK3PXP |
| Врач | doc@compass.demo | Demo-Pass-1 | JBSWY3DPEHPK3PXP |
| Пациенты | p1@compass.demo … p12@compass.demo | Demo-Pass-1 | — |

RED-траектории (демонстрация эскалаций): p2 (Игорь К., GAD-7 → 17, RED по баллу), p3 (Мария Л., PHQ-9, суицидальный пункт на 11-й день), p7 (Елена В., PHQ-9 → 15, RED по баллу).

**Внешний доступ за 0 ₽** (показ жюри, предзащита, «дай потрогать»):

**Основной канал демо — `fxTunnel`** (fxtun.ru, RU-сервис, открытый код):

```bash
cp tools/.fxtun.env.example tools/.fxtun.env   # впиши токен из https://fxtun.ru
./tools/fxtun.sh          # Docker-агент; распечатает публичный https://<sub>.fxtun.ru
./tools/fxtun.sh status
./tools/fxtun.sh --stop
```

Текущий демо-адрес: **https://compass.fxtun.ru**

Адрес — **твой субдомен** (не UUID), стабилен между перезапусками (проверено: `docker restart` и полный `--stop`/`start` возвращают тот же адрес; при выключенном клиенте — 404). Агент запускается **в Docker** обязательно: на этой машине transparent-proxy xray (fake-ip + TUN `throne-tun`) ломает хостовые туннели, а контейнер выходит напрямую через FORWARD. Free-тариф: **без лимитов на запросы и трафик, без таймаута сессий**, 1 активный туннель, свой субдомен бесплатно (0 зарезервированных). Скорость: **200 за 0.43–0.51с** (быстрее ngrok и xTunnel), логины PATIENT/CLINICIAN работают.

⚠️ Интерститиал «Dev Tunnel Warning»: на **первый** браузерный визит показывается анти-фишинговая страница с кнопкой **Continue to site** (один клик; после него cookie `_fxt_consent_<субдомен>` живёт 12 часов). Показывается только для `GET` c `text/html` на субдомен: **JSON/API-запросы, POST и APK не затрагиваются**. Для автоматизации есть заголовок `X-FxTunnel-Skip-Warning: true` (e2e уже шлёт его через `extraHTTPHeaders`). Свой домен (платно) или вход под админом предупреждение убирают.

**Альтернатива — `xTunnel`** (xtunnel.ru, РФ-вход, свой TLS):

```bash
export XTUNNEL_KEY=<ключ>   # из cabinet.xtunnel.ru (FREE_KEY / PERSONAL_KEY / ...)
./tools/xtunnel.sh          # Docker-агент; распечатает стабильный https://<uuid>.tunnel4.com
./tools/xtunnel.sh --stop
```

Адрес: **https://56243ecf-a55c-467a-9098-bfd9cf85af2c.tunnel4.com** (стабилен между рестартами, но free-тариф называет адрес «временным»). Лимиты free: 1 ГБ/сутки, 100 req/мин. В Docker отвечает за ~1с.

**Резерв — `ngrok`** (free, статический dev-домен, авто-TLS; зарубежная инфраструктура):

```bash
./tools/bin/ngrok config add-authtoken <TOKEN>  # разово: token из dashboard.ngrok.com
export NGROK_URL=<ваш>.ngrok-free.app           # dev-домен из dashboard (статичный)
./tools/ngrok.sh          # распечатает стабильный https://<ваш>.ngrok-free.app
./tools/ngrok.sh --stop
```

Резервный адрес: **https://devoted-perch-easily.ngrok-free.app** (статичен между перезапусками; если ISP режет HTTPS до ngrok — гони исходящий через свой Xray: `export NGROK_PROXY=socks5://127.0.0.1:1080`).

⚠️ ngrok free показывает разовую страницу-предупреждение «served for free through ngrok.com» на **первом** заходе браузера (кнопка перехода). API-вызовы (`Accept: application/json`) не перехватываются — приложение работает после одного клика. Убрать интерститиал — заголовок `ngrok-skip-browser-warning` на фронте или платный/свой домен.

**Без аккаунта (эфемерный URL)** — Cloudflare quick tunnel:

```bash
./tools/tunnel.sh          # распечатает публичный https://*.trycloudflare.com
./tools/tunnel.sh status   # показать текущий URL
./tools/tunnel.sh --stop   # остановить
```

Это fallback на случай, когда не хочется регистрироваться. Стек остаётся в Docker Compose на твоей машине, наружу выставляется только UI-порт `:8090` (nginx проксирует `/api/`). Оба туннеля — не WireGuard (TLS до эдж-сервиса), DPI-фильтры их так просто не режут.

⚠️ Туннели подходят только для демо-данных (симулятор) — трафик идёт через зарубежный инфраструктурный сервис. Реальные персональные данные — только на хостинге РФ. У `ngrok` free лимиты ~1 ГБ/мес исходящих и 20k запросов/мес.

## Локальная разработка

```bash
python3 -m venv .venv && .venv/bin/pip install -e "core" -e "api[dev]"
# тестовая БД (dev-override публикует порт на 127.0.0.1; базовый compose — нет)
docker compose -f infra/docker-compose.yml -f infra/docker-compose.dev.yml up -d postgres qdrant
cd api && alembic upgrade head                                # схема из миграций (не create_all)
.venv/bin/pytest core api                                     # 187 + 1000 тестов
cd ui && npm install && npm run dev                           # UI на :5173 (proxy → :8000)
cd e2e && npx playwright install chromium && npx playwright test
```

Изменения схемы — только через `cd api && alembic revision --autogenerate -m "..."` + проверка `alembic upgrade head`. `Base.metadata.create_all` не используется: он не альтерирует существующие таблицы и копит дрейф.


## Пул БД и число воркеров

API работает с PostgreSQL через **async**-драйвер (`sqlalchemy.ext.asyncio` + psycopg).
На каждый HTTP-запрос — ровно одна `AsyncSession` из `app.db.get_db`: она
коммитится/откатывается и закрывается в teardown, соединение возвращается в пул
до ответа клиенту (включая отмену запроса). Синхронный движок (`db.sync_engine`)
остался только для alembic, seed-скриптов и подготовки схемы в тестах.

Каждый процесс API держит собственный пул соединений. Бюджет инсталляции:

```
WEB_CONCURRENCY × (CC_DB_POOL_SIZE + CC_DB_MAX_OVERFLOW + 2) +
CC_DB_BACKGROUND_PROCESSES × (CC_DB_BG_POOL_SIZE + CC_DB_BG_MAX_OVERFLOW + 2) ≤ CC_DB_MAX_CONNECTIONS
```

Слагаемое `+ 2` на процесс — служебные соединения (`init_db`/alembic и sync-сессия seed/CLI). Фоновые
сервисы (`reminders-worker`, `kb-enrichment`) держат маленький пул. Синхронный
движок работает на `NullPool`: request-path его не использует, и он не занимает
соединения в бюджете.

Дефолты (`20+20` на воркер, `2+1` на фоновый процесс, потолок `100`) означают
**2 воркера и 2 фоновых сервиса**: 2×42 + 2×5 = 94 ≤ 100. На старте
`app.db.validate_pool_budget()` падает, если конфигурация не влезает в бюджет:
молчаливой деградации до таймаутов пула нет. Исчерпание пула в рантайме отдаётся
как `503 DB_UNAVAILABLE` с `Retry-After`, а не как 500.

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `WEB_CONCURRENCY` | `1` | число **процессов** uvicorn (`--workers` в образе) |
| `CC_DB_POOL_SIZE` | `20` | постоянных соединений на процесс API |
| `CC_DB_MAX_OVERFLOW` | `20` | пиковых сверх `pool_size` на процесс API |
| `CC_DB_POOL_TIMEOUT` | `30` | сек ожидания свободного соединения (затем `503 DB_UNAVAILABLE`) |
| `CC_DB_MAX_CONNECTIONS` | `100` | потолок Postgres для всей инсталляции |
| `CC_DB_BG_POOL_SIZE` | `2` | соединений на фоновый сервис (напоминания, обогащение) |
| `CC_DB_BG_MAX_OVERFLOW` | `1` | пиковых сверх пула у фонового сервиса |
| `CC_DB_BACKGROUND_PROCESSES` | `2` | сколько фоновых сервисов учитывать в бюджете |

`init_db()` (alembic `upgrade head` + триггер аудита) выполняется под
`pg_advisory_lock`: при `WEB_CONCURRENCY>1` воркеры не гоняются за одни и те же
DDL, а второй дожидается первого.

`uvicorn --workers` — это **многопроцессность, а не многопоточность**: у каждого
процесса своя память. Rate limiter логина (`app.ratelimit`) решает это общим
счётчиком в Redis: в compose сервис `redis` (порт не публикуется) и
`CC_REDIS_URL=redis://redis:6379/0`, режим — `CC_RATE_BACKEND=auto` (Redis при
живом PING, иначе memory с WARNING). При Redis `N` воркеров/реплик тратят
**один** бюджет `CC_RATE_MAX` за окно `CC_RATE_WINDOW`; замер на двух процессах:
5 неудачных входов вперемешку → 429 на 6-й. Без Redis (или при его отказе)
счётчик остаётся per-process: до `N × CC_RATE_MAX` попыток и обнуление при
рестарте — приемлемо для демо/одного процесса, не для нескольких реплик.
`CC_RATE_REDIS_TIMEOUT` (0.3 с) ограничивает ожидание мёртвого Redis, после
чего limiter fail-open переходит в memory (docs/03 §12, docs/10 R24).

## ML-сайдкар (GPU): эмбеддинги, реранк, маскирование ПДн

Тяжёлые модели вынесены в отдельный GPU-сервис `ml` (`ml/`): эмбеддер БЗ
(`qwen3-0.6b` по умолчанию, `e5-large-instruct` альтернативой), cross-encoder
`bge-reranker-v2-m3` и гибридное маскирование ПДн (`compass.depersonalize` +
`rubert-base-pii-ner`). Образ `api` остаётся torch-free; контракт —
`api/app/services/ml_client.py`, детали — [docs/08 §4.1](docs/08-база-знаний.md)
и [ml/README.md](ml/README.md).

```bash
# только на хосте с NVIDIA GPU (иначе профиль gpu не поднимается)
# 1) секреты (включая CC_ML_TOKEN — без него ml не стартует)
tools/gen-secrets.sh infra/.env
# 2) разово скачать веса в том ml-models (единственный шаг с egress)
docker compose -f infra/docker-compose.yml --profile gpu run --rm ml-warmup
# 3) запустить сайдкар: сеть mlnet внутренняя, egress и БД ему недоступны
docker compose -f infra/docker-compose.yml --profile gpu up -d ml
docker compose -f infra/docker-compose.yml --profile gpu logs -f ml
# порт наружу не публикуется: health читаем изнутри контейнера
docker compose -f infra/docker-compose.yml --profile gpu exec ml python -c "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8080/health')))"

# тесты без GPU/torch/token
python -m pytest ml/tests -q
```

Сеть `mlnet` объявлена `internal: true`: `ml` не видит postgres/qdrant/redis и
не имеет выхода в интернет, а api подключён к ней вторым интерфейсом. После
`ml-warmup` сайдкар стартует с `HF_HUB_OFFLINE=1`. Без `--profile gpu` стек не
меняется: `CC_EMBEDDER=hash`, `CC_DEPERSONALIZE_NER=off`; на не-GPU хосте
задайте `CC_ML_URL=` (пусто), чтобы api не ходил в отсутствующий `ml`.
Замеры латентности/VRAM — [REPORT-ml-sidecar.md](REPORT-ml-sidecar.md).

## Обогащение базы знаний (kb-enrichment)

Внешние источники (PubMed, гайдлайны ВОЗ/IRIS) забирает отдельный сервис;
подробности — [docs/15](docs/15-обогащение-базы-знаний.md). В compose он поднимается
как сервис `kb-enrichment` (тот же образ, что `api`, команда
`python -m app.enrichment serve`) и ждёт готовности `api` — миграции применяет API
в своём lifespan.

```bash
cd api
alembic upgrade head                       # таблицы kb_works / kb_runs / kb_exclusions

# один прогон: сначала без записи в БД и Qdrant
CC_ENRICH_PUBMED_EMAIL=you@example.org python -m app.enrichment run --dry-run
CC_ENRICH_PUBMED_EMAIL=you@example.org python -m app.enrichment run
python -m app.enrichment run --source who           # только гайдлайны ВОЗ
python -m app.enrichment runs --limit 5             # история прогонов (воспроизводимость)

# ревью: статьи индексируются только после approve
python -m app.enrichment review list --status candidate
python -m app.enrichment review approve <work_id>
python -m app.enrichment review reject <work_id> --reason "вне скоупа продукта"

# цикл по расписанию (в compose этим занимается сервис kb-enrichment)
python -m app.enrichment serve
```

В контейнере те же команды: `docker compose -f infra/docker-compose.yml exec kb-enrichment /app/.venv/bin/python -m app.enrichment run`.

Переменные (все — `CC_ENRICH_*`, полный список — docs/15 §11):

| Переменная | Обязательна | Смысл |
|---|---|---|
| `CC_DATABASE_URL`, `CC_QDRANT_URL` | да (compose задаёт) | общие с API: реестр и индекс |
| `CC_ENRICH_PUBMED_EMAIL` | желательна | контакт для NCBI; без ключа лимит 3 запроса/с |
| `CC_ENRICH_PUBMED_API_KEY` | нет | ключ NCBI |
| `CC_ENRICH_PUBMED_DAYS` | нет (30) | окно дат публикации |
| `CC_ENRICH_INTERVAL_HOURS` | нет (24) | интервал `serve` |
| `CC_ENRICH_OFFLINE` | нет (false) | `true` — не ходить в сеть (прогон вернёт `failed` с пояснением) |

Ревью также доступно по HTTP (`/api/v1/admin/kb/*`, роль ADMIN) — docs/04 §3.1.

## CI и релизы (локально; GitHub — только хранилище APK)

Лимиты GitHub Actions исчерпаны, поэтому тесты и сборки гоняются у нас,
а GitHub используется как место, куда кладётся готовый APK.

```bash
./tools/ci.sh            # ядро (cov ≥95%) + API + ml/tests + docker build
./tools/ci.sh --e2e      # + полный Playwright-прогон
./tools/ci.sh --skip-docker
```

Интеграционные прогоны (api-тесты и e2e) идут в **постоянном стенде**
`tools/dev-stage.sh` — compose-проект `cc-dev-stage` с Postgres, Qdrant, redis,
api, ui, reminders-worker, kb-enrichment и remd-mock (профиль `emias` — e2e документа приёма):

```bash
./tools/dev-stage.sh up          # поднять/обновить стенд
./tools/dev-stage.sh test        # api-тесты + e2e в стенде
./tools/dev-stage.sh test --api  # только api-тесты
./tools/dev-stage.sh status      # что запущено, точки в КБ, отпечатки
./tools/dev-stage.sh down        # остановить, данные сохранить
./tools/dev-stage.sh reset       # снести вместе с данными
```

Стенд слушает только loopback: Postgres `127.0.0.1:55432`, Qdrant
`127.0.0.1:56333`, UI `127.0.0.1:8097` (`CC_STAGE_PG_PORT` /
`CC_STAGE_QDRANT_PORT` / `CC_STAGE_UI_PORT`), туннеля на него нет. Рабочий
Postgres `:5432` (база `compass`) и Qdrant `:6333` (`kb`, `kb_600`) недостижимы:
у стенда свои контейнеры и тома, а api-тесты работают в отдельной БД
`compass_test` с коллекцией `kb_test`, тогда как стек стенда живёт в
`compass`/`kb_stage`.

Стенд постоянный намеренно: Postgres, демо-данные и индекс КБ переживают
прогоны, поэтому индекс не пересчитывается каждый раз. Пересборка образов и
переиндексация выполняются только при изменении входов — отпечатки лежат в
`.stage/`: `images.sha` (код api/core/ui/ml/infra), `kb.sha` (источники корпуса,
эмбеддер, геометрия чанков). `./tools/dev-stage.sh seed` переиндексирует
принудительно.

Внешние интеграции в стенде включены и смотрят на моки профиля `emias`
(`emias-mock`, `remd-mock`): `CC_EMIAS_ENABLED=true`,
`CC_EMIAS_ALLOW_INSECURE=true`, `CC_REMD_ENABLED=true`. Без них спек
«протокол подписывается, проверяется, печатается и регистрируется в РЭМД»
(docs/18, FR-39…FR-41) проверяет только выключенное состояние и падает.
Моки не публикуют порты на хост — трафик остаётся во внутренней сети.

Релиз (тесты → образы → APK → тег → GitHub Release с APK):

```bash
./tools/release-local.sh 1.1.0                   # полный цикл
./tools/release-local.sh 1.1.0 --skip-ci         # тесты уже гоняли
./tools/release-local.sh 1.1.0 --no-images       # только APK + Release
./tools/release-local.sh 1.1.0 --push-images     # + docker push в GHCR
```

`versionName` и `versionCode` считаются из `X.Y.Z` → `X*10000+Y*100+Z`
(`1.1.0` → `10100`); тег `vX.Y.Z` создаётся только из чистого дерева.
APK заливается `gh release upload` — для этого достаточно `gh` со scope `repo`.
Публикация образов в GHCR — отдельный scope `write:packages`: в текущем
`gh`-токене его нет, включить можно так: `gh auth refresh -h github.com -s write:packages`
(или PAT с `write:packages` → `docker login ghcr.io`). Без него образы остаются
локальными, а `tools/release-local.sh` их просто не пушит.

GitHub Actions остаются резервным путём, основной релиз идёт из
`tools/release-local.sh`:

- `.github/workflows/ci.yml` — те же ворота на стороне GH (когда биллинг вернётся);
  канонический CI — `tools/ci.sh`.
- `.github/workflows/release-android.yml` — сборка APK на раннере
  (`workflow_dispatch`), прикрепляет APK к существующему Release.
- Публикация образов в GHCR на тег удалена (`release.yml`): образы собираются
  локально, тег и Release создаёт `tools/release-local.sh`.

## Мобильное приложение (Android APK, item 4)

`mobile/` — Capacitor-обёртка веб-клиента (`ui/`), universal APK `dev.compass.assistant`:

```bash
./tools/build-apk.sh 1.1.0     # SPA (VITE_API_BASE) → cap sync → gradlew assembleRelease
./tools/build-apk.sh           # versionName из build.gradle, APK в dist/compass-release.apk
```

- **API-хост**: сборка веб-части идёт с `VITE_API_BASE=<публичный https-URL API>` (в нативном приложении нет same-origin). Стабильный демо-адрес — `https://compass.fxtun.ru` (см. «Внешний доступ»); переопределяется `CC_API_BASE`.
- **Пуши**: нативно приложение использует не Web Push, а FCM. Регистрация токена устройства: `POST /api/v1/push/device-token`, одноразовый апгрейд `POST /api/v1/push/fcm/upgrade` (cooldown 60 мин → `429 FCM_COOLDOWN`, статус в колоколе уведомлений).
- **Firebase**: для реальной доставки пушей нужен Firebase-проект (`google-services.json` в `mobile/android/app/`); без файла APK собирается, но пуши не работают. Серверная отправка — FCM HTTP v1 по `CC_FCM_SERVICE_ACCOUNT_B64` (base64 service-account JSON); без неё сервер шлёт только Web Push.
- **Подпись**: `tools/build-apk.sh` генерирует персистентный demo-keystore (`mobile/android/app/release.keystore`, `CN=Compass Demo`) и подписывает release-APK; этот же сертификат у APK из GitHub Release, поэтому обновления ставятся поверх.
- **Локальная сборка** (без локальных Java/SDK): `tools/build-apk.sh` работает в образе `compass-android-builder` (JDK 21 + Android SDK 36), сам собирает SPA, делает `npx cap sync android`, запускает `gradlew assembleRelease` с `-PccVersionName/-PccVersionCode` и кладёт APK в `dist/compass-<версия>-release.apk`, печатая `package:` из `aapt2`.

## ИИ-ассистент врача: LLM роутит раздел и пишет ответ по сниппетам

Поиск ассистента (`/assistant/ask`) идёт по базе знаний в 4 разделах (ШКАЛЫ/ТРИАЖ/РЕКОМЕНДАЦИИ/ЛЕКАРСТВА). Раздел **не выбирается пользователем**: LLM-роутер получает очищенный от ПДн запрос и вызывает тулзу `search_<раздел>` (docs/04 FR-17, docs/08 §9). Ответ по-русски пишет LLM по найденным сниппетам и истории треда (`llm_router.compose_answer`): ссылки `[n]` проверяются на сервере, `citations` собираются только из реальных фрагментов; карта пациента в модель не уходит, диагноз/терапия (красные линии) отказываются детерминированно. Без LLM, при сбое модели или невалидных ссылках ответ строит детерминированный `build_rag_answer`. Если LLM недоступен/ошибся/вопрос вне разделов — поиск по всем разделам (fallback).

```bash
# 1) Локальный llama.cpp-сервер (OpenAI-совместимый /v1; модель 27B на RTX 5090)
./tools/llm.sh            # печатает http://127.0.0.1:18080/v1
./tools/llm.sh status
./tools/llm.sh --stop
# 2) Подключить к стеку: в infra/.env
#    CC_ASSISTANT_LLM_URL=http://host.docker.internal:18080/v1
#    CC_ASSISTANT_LLM_MODEL=local
#    затем: docker compose -f infra/docker-compose.yml up -d api
#
# Вариант с omniroute (OpenAI-совместимый шлюз с ключом):
#    шлюз слушает loopback хоста, контейнер его не видит; пробрасываем relay
#    (socat) в сеть шлюза и публикуем на docker-шлюзе. <сеть> — сеть, в которой
#    живёт контейнер omniroute (docker inspect <контейнер> → NetworkSettings.Networks),
#    у hermes-стека это hermes-docker_default; <контейнер> — его имя.
#    docker run -d --restart unless-stopped --name omniroute-relay \
#      --network <сеть> -p 172.17.0.1:20133:20128 \
#      alpine/socat:1.8.0.0 TCP-LISTEN:20128,fork,reuseaddr TCP:<контейнер>:20128
#    # и в infra/.env (compose прокидывает эти переменные в api)
#    CC_ASSISTANT_LLM_URL=http://172.17.0.1:20133/v1
#    CC_ASSISTANT_LLM_MODEL=<id модели, напр. opencode-go/deepseek-v4.1-flash>
#    CC_ASSISTANT_LLM_API_KEY=<персональный ключ omniroute>
#    CC_ASSISTANT_LLM_TIMEOUT=30   # модели с reasoning отвечают 4-20 c
#    Порог 172.17.0.1 виден и другим контейнерам хоста, поэтому ключ omniroute
#    должен быть ограничен по моделям; публикуйте порт только на docker-шлюзе.
```

Без `CC_ASSISTANT_LLM_URL` LLM выключен (эквивалент «Все разделы», детерминированный ответ) — весь стек и e2e работают без LLM.

**Локальная модель в контуре** (без внешнего egress, 152-ФЗ): оверлей `infra/docker-compose.local-llm.yml` поднимает llama.cpp в сети `infra_default`, api обращается к `http://local-llm:8080/v1`. Прод-дефолт не меняется — `CC_ASSISTANT_LLM_URL` остаётся пустым. Команды, выбор модели и проверка — [docs/17](docs/17-локальная-модель.md).

## Дорожная карта

- [x] Бизнес-анализ
- [x] Системный анализ
- [x] Архитектура
- [x] Спецификация
- [x] SDD
- [x] TDD
- [x] E1–E2: клинический движок (187 юнит-тестов)
- [x] E3–E5: API (1000 интеграционных тестов)
- [x] E6: UI, симулятор, docker compose, e2e (62 Playwright)
- [x] Кейс-дефициты P1–P9: Авиандр в KB, маршрутизация, НЯ, приверженность, парсинг историй, врач↔пациент чат, КР Минздрава
- [x] ИИ-ассистент врача: раздел БЗ выбирает LLM-роутер (тулколлинг search_<раздел>, fallback все разделы)
- [x] Security audit F1–F12 + NEW-1..5: pgcrypto на всех free-text ПДн, уничтожение при отзыве, ротация refresh, httpOnly cookies, fail-fast секреты, AES-256 backup, threat model (ГОСТ 14971), gate привилегированной регистрации

**Регрессия:** 187 core + 1000 API + 62 e2e = 1249 тестов.

**Первый администратор (прод):** публичная регистрация CLINICIAN/ADMIN запрещена вне демо-режима (NEW-1):
`docker compose -f infra/docker-compose.yml run --rm --entrypoint /app/.venv/bin/python api -m app.bootstrap_user --email admin@clinic.ru --role ADMIN`

Ключевые ограничения: см. красные линии в 01 §4 и 02 §7.2 (без «автодиагноза», без биометрии, 152-ФЗ).
Открытые вопросы метрик — 15.10 (открытие чемпионата).