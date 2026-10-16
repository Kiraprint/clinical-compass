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

## Структура

```
core/   — клинический движок (SDK): шкалы, скоринг, триаж, риск-флаги, чек-ины, тренды (чистый Python)
api/    — FastAPI: auth/2FA, RBAC, consent (152-ФЗ), аудит (append-only), скрининг, эскалации,
          дневник, чат, панель врача, ростер, симулятор демо-данных
ui/     — React + Vite: пациент (скрининг/чек-ин/дневник/чат/тренд), панель врача, админ-аудит
infra/  — docker compose (postgres + api + ui + seed)
e2e/    — Playwright: питч-сценарий (docs/04 §6)
docs/   — ТЗ (ГОСТ 19.201-78) + 01–06 (разработочная документация), карта соответствия ГОСТ 19
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
docker compose -f infra/docker-compose.yml up -d postgres   # тестовая БД
cd api && alembic upgrade head                                # схема из миграций (не create_all)
.venv/bin/pytest core api                                     # 41 + 54 теста
cd ui && npm install && npm run dev                           # UI на :5173 (proxy → :8000)
cd e2e && npx playwright install chromium && npx playwright test
```

Изменения схемы — только через `cd api && alembic revision --autogenerate -m "..."` + проверка `alembic upgrade head`. `Base.metadata.create_all` не используется: он не альтерирует существующие таблицы и копит дрейф.

## CI и релизы

Локально (тесты ядра + API против реального Postgres + сборка образов; опционально `--e2e` — полный стек + Playwright):

```bash
./tools/ci.sh            # тесты + сборка
./tools/ci.sh --e2e      # + подъём стека и e2e-прогон
```

GitHub Actions (запускаются после пуша в репозиторий; сейчас remote не задан):

- `.github/workflows/ci.yml` — на push/PR: тесты ядра и API (Postgres в service-контейнере) + сборка docker-образов; e2e-джоб на `master`/`main`.
- `.github/workflows/release.yml` — на тег `v*`: сборка и публикация образов в GHCR (`compass-api`, `compass-ui`) + черновой GitHub Release.
- `.github/workflows/release-android.yml` — универсальный Android APK (Capacitor + FCM) из веб-клиента `mobile/`; APK — артефакт (на `v*` — в релиз).

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

## Дорожная карта

- [x] Бизнес-анализ
- [x] Системный анализ
- [x] Архитектура
- [x] Спецификация
- [x] SDD
- [x] TDD
- [x] E1–E2: клинический движок (33 юнит-теста)
- [x] E3–E5: API (36 интеграционных тестов)
- [x] E6: UI, симулятор, docker compose, e2e (Playwright)

Ключевые ограничения: см. красные линии в 01 §4 и 02 §7.2 (без «автодиагноза», без биометрии, 152-ФЗ).
Открытые вопросы метрик — 15.10 (открытие чемпионата).