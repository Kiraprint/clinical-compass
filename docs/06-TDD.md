# 06. TDD — тесты по контрактам

Статус: v0.1, 2026-10-09. Рабочий цикл: **red** (тест падает с ожидаемой причиной) → **green** (минимальная реализация) → **refactor** (при зелёном). Регрессия — полный прогон перед каждым коммитом.

---

## 1. Стратегия

- Пирамида (фактически достигнуто, см. `core/tests` + `api/tests`): unit (ядро) ~41, integration (API+БД) ~54, e2e (UI-флоу) 11. Числа в 05/06 задавались как ориентир; приоритет — честная трассировка, а не набивка количества.
- Ядро: 100% детерминированные чистые функции — табличные параметризованные тесты порогов.
- Интеграция: реальные HTTP-запросы к тестовому Postgres (отдельная БД), не мок-эхо.
- Запрещены тесты «на копирование»: только поведение контрактов (вход → выход/инвариант/ошибка).
- Источники истины: 04 (FR/AC), 05 (контракты ядра), 02 §6 (инварианты БД).

## 2. Юнит-тесты ядра (core/tests)

### 2.1 scoring (PHQ-9)

| Тест | Given | When | Then |
|---|---|---|---|
| score_sums_items | answers {0:0,…,8:2} | score() | total = sum; per_item совпадает |
| score_unknown_item_raises | ответ с item_key="zzz" | score() | UnknownItemError |
| severity_none | total 0–4 | score() | NONE |
| severity_mild | total 5–9 | score() | MILD |
| severity_moderate | total 10–14 | score() | MODERATE |
| severity_severe | total 15–27 | score() | SEVERE |
| severity_boundary_5_9_10 | total 4,5,9,10,14,15 | score() | пограничные значения строго по порогам |

> Пороги PHQ-9: 0–4 NONE, 5–9 MILD, 10–14 MODERATE, 15–27 SEVERE. GAD-7: 0–4/5–9/10–14/15+. HADS: 0–7/8–10/11–21 (по подшкалам). **Подтвердить медиком команды [TBD]** — клинические числа: пороги severity, порог суицидального флага (item 9 ≥ 1), окна тренда (7/7/30 дней).

### 2.2 risk_flags и triage

| Тест | Given | When | Then |
|---|---|---|---|
| phq9_suicidal_item_flags | PHQ-9 item 9 = 1 | risk_flags() | SUICIDAL_IDEATION присутствует (item_key=q9) |
| phq9_green_no_flags | PHQ-9 total 0, item 9 = 0 | risk_flags() | флагов нет |
| phq9_severe_symptoms_flag_at_boundary | PHQ-9 total 15 / 14 | risk_flags() | SEVERE_SYMPTOMS при 15, нет при 14 |
| phq9_severe_symptoms_with_nonzero_q9 | total 15 + item 9 = 1 | risk_flags() | оба флага |
| gad7_no_suicidal_flag_but_severe_symptoms | GAD-7 total 15 | risk_flags() | SUICIDAL_IDEATION нет, SEVERE_SYMPTOMS есть |
| hads_has_no_severe_bound_no_severe_flag | HADS-A/HADS-D максимум баллов | risk_flags() | SEVERE_SYMPTOMS нет (потолок MODERATE) |
| gad7_severity_boundaries | GAD-7 total 0/5/10/15 | score() | NONE/MILD/MODERATE/SEVERE |
| triage_red_suicidal | flag SUICIDAL, severity MILD | triage() | RED (флаг важнее severity) |
| triage_red_severe | severity SEVERE, флагов нет | triage() | RED |
| triage_yellow | severity MODERATE, флагов нет | triage() | YELLOW |
| triage_green | severity NONE, флагов нет | triage() | GREEN |
| triage_deterministic_input_output_table | таблица вход/выход по границам | triage() | GREEN/YELLOW/RED по таблице |

### 2.3 checkin (генерация)

| Тест | Given | When | Then |
|---|---|---|---|
| checkin_len_2_5 | last = None / last = ScaleResult(MODERATE) | generate_checkin() | 2 ≤ items ≤ 5 |
| test_checkin_can_reach_five_items | 5+ worst-пунктов + суицидальный | generate_checkin() | items == 5 (потолок достижим) |
| checkin_focuses_worst | per_item имеет worst-пункт (макс балл) | generate_checkin() | worst-пункт входит в вопросы |
| checkin_bounded | любой вход | generate_checkin() | item_keys ⊆ keys шкалы |
| checkin_deterministic | last = None / worst q2 | generate_checkin() | конкретный набор: [q1,q2,q3] / [q2,q9] |

### 2.4 trend

| Тест | Given | When | Then |
|---|---|---|---|
| trend_empty_input | runs = [] | trends() | пустой тренд, без исключений |
| trend_delta_30 | 30-дневная серия с известной разницей | trends() | delta_30 = конец − начало |
| trend_moving_avg | серия [1,2,3,4,5] окно 7 | trends() | avg = расширяющееся среднее: длина ряда та же, значения = (1/1, 3/2, 6/3, 10/4, 15/5); окно > длины ряда — не ошибка, а полное среднее |

## 3. Интеграционные тесты (api/tests, реальный Postgres)

| Тест | Сценарий | Ожидание |
|---|---|---|
| auth_2fa_required | регистрация CLINICIAN → login без otp | 401; с корректным TOTP → 200 |
| consent_gating | PATIENT без согласия → POST /screenings | 403 CONSENT_REQUIRED; после /consent → 201 |
| screening_closes_run | finish → повторные answers | 409 RUN_CLOSED |
| screening_one_open_only | 2-й screenings при открытом | 409 CONFLICT |
| escalation_under_1s | красный ответ → POST finish | 201; escalation создан; время полного ответа < 1 с |
| escalation_notifies | RED с суицид. флагом | уведомление врачу в БД (escalations.row) + контакты помощи в ответе |
| checkin_once_per_day | 2-й чек-ин в тот же день | 409 RUN_ALREADY_TODAY |
| roster_enforcement | врач без roster → GET patients/{id} | 403 NO_ROSTER; после /admin/roster → 200 |
| patient_cannot_read_others | PATIENT → GET diary другого пациента / report | 403 |
| audit_append_only | прямой UPDATE/DELETE audit_log на уровне БД | ошибка БД-триггера (исключение) — отдельный БД-тест, вне HTTP-слоя (API не предоставляет UPDATE/DELETE-эндпоинтов для audit_log) |
| audit_truncate_blocked | прямой TRUNCATE audit_log на уровне БД | ошибка БД-триггера (statement-уровень `trg_audit_no_truncate`) |
| audit_records_access | чтение report | в audit_log появилась запись (actor, action='READ_REPORT') |
| consent_revoke | revoke → повторное обращение к медданным | 403 CONSENT_REQUIRED |
| questionnaire_catalog | GET /questionnaires (FR-2) | каталог шкал PHQ-9/GAD-7/HADS-A/HADS-D из core; тексты не дублируются в UI |
| checkin_out_of_range | значение ответа чек-ина вне 0..max_value | 422 VALIDATION (не 500) |
| checkin_daily_unique_index | 2 CHECKIN в один день МСК на уровне БД | уникальный индекс `runs_checkin_daily` отклоняет второй (TOCTOU-защита) |
| screening_abandon | открытый скрининг → POST /screenings (409) → /screenings/abandon → снова 201 | повторный старт возможен после abandon |
| abandon_no_open_run | abandon при отсутствии открытого скрининга | 200 `{abandoned: false}` (идемпотентно) |
| login_rate_limited | > rate_max попыток входа с (IP, email) | 429 TOO_MANY_REQUESTS |
| patient_cannot_choose_scale | POST /screenings с `{questionnaire_code:"GAD-7"}` | сервер игнорирует и применяет назначенную шкалу (дефолт PHQ-9), item 1 |
| prescribed_scale_defaults_to_phq9 | пациент без назначения | GET /me/prescribed-scale → `{questionnaire_code:"PHQ-9"}` |
| prescribe_scale_roster_gated | врач без roster назначает шкалу | 403 NO_ROSTER |
| prescribe_scale_applies_to_patient | PUT prescribed-scale (GAD-7) → /me/prescribed-scale и report | пациент видит GAD-7; report несёт `prescribed_scale` |
| red_escalation_notifies_rostered_clinician | RED-скрининг пациента из roster | у врача в roster создано Notification (kind=RED_ESCALATION, unread=1) |
| red_escalation_no_roster_no_notification | RED-скрининг пациента без roster | врач не получает уведомление (items пусто) |
| notifications_mark_read_individual | POST /me/notifications/read {ids} | уведомление помечено прочитанным, unread=0 |
| push_subscribe_unsubscribe | VAPID-ключ, подписка/отписка по endpoint | подписка апсерт по endpoint; чужая подписка не удаляется; без токена 401 |
| fcm_device_token_register_delete | POST/DELETE /push/device-token | апсерт по токену; чужой токен не удаляется; без токена 401 |
| fcm_upgrade_then_cooldown | POST /push/fcm/upgrade дважды | первый 200 `{upgraded}`; второй 429 `FCM_COOLDOWN`; /push/fcm/status окно активно |

## 4. e2e (UI-флоу, playwright против docker compose, `e2e/tests/`)

`pitch.spec.ts` — питч-сценарий (docs/04 §6):
1. Пациент: скрининг с суицидальным ответом (PHQ-9 item 9 = 3) → RED + контакты помощи (8-800-2000-122, 112).
2. Пациент с RED-историей: чат с горячей линией отвечает контактами.
3. Врач: вход с 2FA (TOTP), фильтр RED, открытие отчёта первого RED-пациента.

`patient-flows.spec.ts` — первый запуск и ежедневные действия (недостижимы на предсозданных пациентах симулятора, у которых согласие уже есть):
4. Регистрация нового пациента → экран согласия (152-ФЗ) → подпись → скрининг доступен; шкала скрининга пациента не выбирает (item 1 — нет `<select>`, отображается назначенная врачом шкала).
5. Ежедневный чек-ин: вопросы (нет истории → первые 3 пункта шкалы) → ответы → «Чек-ин за сегодня выполнен».
6. Дневник: создание записи появляется в списке.
7. Тренд пациента: SVG-график и «Последний балл» при наличии истории.

`clinician-admin.spec.ts` — клинический и административный интерфейс:
8. Врач: фильтр по триажу (GREEN и YELLOW).
9. Врач: отчёт RED-пациента — открытые эскалации, история скринингов, дневник, графика динамики.
10. Админ: журнал аудита (append-only) содержит записи.
11. Врач: уведомление при RED-эскалации — колокол в шапке, детерминированная API-подготовка (свежий врач/пациент, roster через админа, RED-скрининг), «Прочитать все».

e2e-хелперы вынесены в `e2e/tests/helpers.ts` (TOTP RFC 4648 base32 + HMAC-SHA1, `login`, `registerPatient`, `freshPatientEmail`).

## 5. Регрессия и CI

- Подготовка (единый venv): `pip install -e "core[dev]" -e "api[dev]"`; тестовый Postgres — `docker compose -f infra/docker-compose.yml up -d postgres` (тестовая БД `compass_test` создаётся фикстурой, URL в `CC_TEST_DATABASE_URL`).
- Команда прогона: `pytest core api -q` (интеграция — против реального Postgres, не моков).
- Покрытие ядра (DoD E1, ≥95%): `pytest core --cov=compass --cov-fail-under=95 --cov-report=term-missing` (нужен `pytest-cov` в dev-extra).
- e2e (UI-флоу): `cd e2e && npx playwright test` — запускать при изменении `ui/` или контрактов API; против поднятого `docker compose up -d postgres api ui`.
- Перед каждым коммитом — полный прогон `pytest core api -q` + (при изменении UI) e2e.

## 6. Ключевые негативные проверки (обязательны к демо)

- Суицидальный пункт не «теряется» при высокой общей сумме и наоборот (таблица 2.2).
- Чек-ин не генерирует вопросов вне шкалы (защита от «мусорной» генерации).
- Аудит нельзя подделать (append-only триггер).
- Доступ врача строго по roster (даже при верном JWT).