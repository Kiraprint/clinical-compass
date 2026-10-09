# 06. TDD — тесты по контрактам

Статус: v0.1, 2026-10-09. Рабочий цикл: **red** (тест падает с ожидаемой причиной) → **green** (минимальная реализация) → **refactor** (при зелёном). Регрессия — полный прогон перед каждым коммитом.

---

## 1. Стратегия

- Пирамида: unit (ядро) ~60, integration (API+БД) ~20, e2e (UI-флоу) ~5.
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

> Пороги PHQ-9: 0–4 NONE, 5–9 MILD, 10–14 MODERATE, 15–27 SEVERE. GAD-7: 0–4/5–9/10–14/15+. HADS: 0–7/8–10/11–21 (по подшкалам). **Подтвердить медиком команды [TBD]** — единственные числа, которые может поменять клиника.

### 2.2 risk_flags и triage

| Тест | Given | When | Then |
|---|---|---|---|
| suicidal_item_flags | PHQ-9 item 9 = 1 | risk_flags() | SUICIDAL_IDEATION присутствует |
| suicidal_zero_no_flag | item 9 = 0 | risk_flags() | флага нет |
| triage_red_suicidal | flag SUICIDAL, severity MILD | triage() | RED (флаг важнее severity) |
| triage_red_severe | severity SEVERE, флагов нет | triage() | RED |
| triage_yellow | severity MODERATE, флагов нет | triage() | YELLOW |
| triage_green | severity NONE, флагов нет | triage() | GREEN |
| triage_deterministic | одинаковый вход дважды | triage() | идентичный результат (без рандома) |

### 2.3 checkin (генерация)

| Тест | Given | When | Then |
|---|---|---|---|
| checkin_len_2_5 | last = None / last = ScaleResult(MODERATE) | generate_checkin() | 2 ≤ items ≤ 5 |
| checkin_focuses_worst | per_item имеет worst-пункт (макс балл) | generate_checkin() | worst-пункт входит в вопросы |
| checkin_bounded | любой вход | generate_checkin() | item_keys ⊆ keys шкалы |

### 2.4 trend

| Тест | Given | When | Then |
|---|---|---|---|
| trend_empty_input | runs = [] | trends() | пустой тренд, без исключений |
| trend_delta_30 | 30-дневная серия с известной разницей | trends() | delta_30 = конец − начало |
| trend_moving_avg | серия [1,2,3,4,5] окно 7 | trends() | avg не меняет длину ряда |

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
| audit_append_only | попытка UPDATE/DELETE audit_log | ошибка БД-триггера (500 → тест на уровне БД: ожидаем исключение) |
| audit_records_access | чтение report | в audit_log появилась запись (actor, action='READ_REPORT') |
| consent_revoke | revoke → повторное обращение к медданным | 403 CONSENT_REQUIRED |

## 4. e2e (UI-флоу, playwright против docker compose)

1. `seed` → пациент веб-флоу: согласие → скрининг → жёлтый/зелёный тренд; врач видит карточку.
2. Красный флаг от симулятора → у врача в панели первым в списке с бейджем RED.
3. Питч-сценарий 04 §6 целиком (5 шагов) за один прогон.

## 5. Регрессия и CI

- Команда прогона: `pytest core api` + `playwright test` (e2e, только при изменении UI).
- Перед коммитом — полный прогон; в CI — Docker build + прогон (локально на демо-ноде).
- Новая логика (порог, правило) → сначала тест-таблица, красный → реализация.

## 6. Ключевые негативные проверки (обязательны к демо)

- Суицидальный пункт не «теряется» при высокой общей сумме и наоборот (таблица 2.2).
- Чек-ин не генерирует вопросов вне шкалы (защита от «мусорной» генерации).
- Аудит нельзя подделать (append-only триггер).
- Доступ врача строго по roster (даже при верном JWT).