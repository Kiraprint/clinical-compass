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
.venv/bin/pytest core api                                     # 80 + 111 тестов
cd ui && npm install && npm run dev                           # UI на :5173 (proxy → :8000)
cd e2e && npx playwright install chromium && npx playwright test
```

Изменения схемы — только через `cd api && alembic revision --autogenerate -m "..."` + проверка `alembic upgrade head`. `Base.metadata.create_all` не используется: он не альтерирует существующие таблицы и копит дрейф.

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

## CI и релизы

Локально (тесты ядра + API против реального Postgres + сборка образов; опционально `--e2e` — полный стек + Playwright):

```bash
./tools/ci.sh            # тесты + сборка
./tools/ci.sh --e2e      # + подъём стека и e2e-прогон
```

GitHub Actions (`origin` — `Kiraprint/clinical-compass`):

- `.github/workflows/ci.yml` — на push/PR: тесты ядра и API (Postgres в service-контейнере) + сборка docker-образов; e2e-джоб на `master`/`main`.
- `.github/workflows/release.yml` — на тег `v*`: публикация образов в GHCR (`compass-api`, `compass-ui` → GitHub Packages), создание GitHub Release (не draft), затем вызов APK-джоба.
- `.github/workflows/release-android.yml` — Android APK (Capacitor + FCM): на push `main`/`master` и `workflow_dispatch` — artifact `compass-apk`; на теге `v*` вызывается из `release.yml` и прикрепляет APK к тому же Release.

Выпуск версии: `git tag -a vX.Y.Z -m "..." && git push origin vX.Y.Z`.
Из тега берутся `versionName` (`X.Y.Z`) и `versionCode` (`X*10000+Y*100+Z`); сборки из веток получают `1.0-<sha>` и `1000+run_number`.

## Мобильное приложение (Android APK, item 4)

`mobile/` — Capacitor-обёртка веб-клиента (`ui/`), universal APK `dev.compass.assistant`:

```bash
cd ui && VITE_API_BASE=https://compass.fxtun.ru npm run build   # SPA с адресом демо
cd ../mobile && npm ci && npx cap sync android  # скопировать в нативный проект
cd android && ./gradlew assembleDebug           # APK: app/build/outputs/apk/debug/
```

- **API-хост**: сборка веб-части идёт с `VITE_API_BASE=<публичный https-URL API>` (в нативном приложении нет same-origin). Стабильный демо-адрес — `https://compass.fxtun.ru` (см. «Внешний доступ»); в CI это `vars.VITE_API_BASE`.
- **Пуши**: нативно приложение использует не Web Push, а FCM. Регистрация токена устройства: `POST /api/v1/push/device-token`, одноразовый апгрейд `POST /api/v1/push/fcm/upgrade` (cooldown 60 мин → `429 FCM_COOLDOWN`, статус в колоколе уведомлений).
- **Firebase**: для реальной доставки пушей нужен Firebase-проект (`google-services.json` в `mobile/android/app/`, в CI — из `secrets.FCM_GOOGLE_SERVICES_JSON`); без файла APK собирается, но пуши не работают. Серверная отправка — FCM HTTP v1 по `CC_FCM_SERVICE_ACCOUNT_B64` (base64 service-account JSON); без неё сервер шлёт только Web Push.
- **Подпись**: debug-APK подписывается автоматически; release — из `ANDROID_KEYSTORE_B64` + паролей (см. workflow).
- **Локальная сборка release-APK через CLI** (без локальных Java/SDK): `tools/build-apk.sh` собирает образ `compass-android-builder` (JDK 21 + Android SDK 36), генерирует персистентный demo-keystore, запускает `gradlew assembleRelease` и кладёт подписанный APK в `dist/compass-release.apk`. Перед запуском пересобери веб-часть с `VITE_API_BASE` и `npx cap sync android` (см. блок выше).

## ИИ-ассистент врача: раздел БЗ выбирает LLM

Поиск ассистента (`/assistant/ask`) идёт по базе знаний в 4 разделах (ШКАЛЫ/ТРИАЖ/РЕКОМЕНДАЦИИ/ЛЕКАРСТВА). Раздел **не выбирается пользователем**: LLM-роутер получает очищенный от ПДн запрос и вызывает тулзу `search_<раздел>` (docs/04 FR-17, docs/08 §9). Ответ всегда строит детерминированный `build_rag_answer` — LLM не отвечает клинически (красные линии). Если LLM недоступен/ошибся/вопрос вне разделов — поиск по всем разделам (fallback).

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

Без `CC_ASSISTANT_LLM_URL` роутер выключен (эквивалент «Все разделы») — весь стек и e2e работают без LLM.

**Локальная модель в контуре** (без внешнего egress, 152-ФЗ): оверлей `infra/docker-compose.local-llm.yml` поднимает llama.cpp в сети `infra_default`, api обращается к `http://local-llm:8080/v1`. Прод-дефолт не меняется — `CC_ASSISTANT_LLM_URL` остаётся пустым. Команды, выбор модели и проверка — [docs/17](docs/17-локальная-модель.md).

## Дорожная карта

- [x] Бизнес-анализ
- [x] Системный анализ
- [x] Архитектура
- [x] Спецификация
- [x] SDD
- [x] TDD
- [x] E1–E2: клинический движок (80 юнит-тестов)
- [x] E3–E5: API (142 интеграционных теста)
- [x] E6: UI, симулятор, docker compose, e2e (26 Playwright)
- [x] Кейс-дефициты P1–P9: Авиандр в KB, маршрутизация, НЯ, приверженность, парсинг историй, врач↔пациент чат, КР Минздрава
- [x] ИИ-ассистент врача: раздел БЗ выбирает LLM-роутер (тулколлинг search_<раздел>, fallback все разделы)
- [x] Security audit F1–F12 + NEW-1..5: pgcrypto на всех free-text ПДн, уничтожение при отзыве, ротация refresh, httpOnly cookies, fail-fast секреты, AES-256 backup, threat model (ГОСТ 14971), gate привилегированной регистрации

**Регрессия:** 88 core + 143 API + 33 e2e = 264 теста.

**Первый администратор (прод):** публичная регистрация CLINICIAN/ADMIN запрещена вне демо-режима (NEW-1):
`docker compose -f infra/docker-compose.yml run --rm --entrypoint /app/.venv/bin/python api -m app.bootstrap_user --email admin@clinic.ru --role ADMIN`

Ключевые ограничения: см. красные линии в 01 §4 и 02 §7.2 (без «автодиагноза», без биометрии, 152-ФЗ).
Открытые вопросы метрик — 15.10 (открытие чемпионата).