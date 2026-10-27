# 06. TDD — тесты по контрактам

Статус: v0.1, 2026-10-09. Рабочий цикл: **red** (тест падает с ожидаемой причиной) → **green** (минимальная реализация) → **refactor** (при зелёном). Регрессия — полный прогон перед каждым коммитом.

---

## 1. Стратегия

- Пирамида (фактически достигнуто на 2026-10-26, см. `core/tests` + `api/tests` + `ml/tests` + `e2e/tests`): unit (ядро) 187, integration (API+БД) 1000 (2 skip по окружению), из них: 78 — обогащение КБ, FR-20; 18 — rate limiter на общем Redis-счётчике, docs/03 §12; 43 — деперсонализация/NER/remote, docs/12; 9 — клиент ML-сайдкара, docs/08 §4; 10 — пациентский кабинет и FR-34 в `test_patient_ux.py`; 24 — retrieval: чанкер/окно реранка/remote-эмбеддер/порог пула, docs/08 §3-4; 8 — персональное ревью статей врачом, FR-36; 14 — анамнез и изоляция росписи, FR-37; 42 — календарь приёмов, сессии и протокол приёма, FR-38/39 (`test_sessions_*.py`); 31 — ИИ-ассистент сессии: аудио/расшифровка/деперсонализация/черновик, FR-39 (`test_session_ai.py`); 23 — подпись и PDF протокола, FR-40 (`test_protocol_sign.py`, `test_protocol_pdf.py`); 45 — СЭМД в РЭМД/ЕМИАС через мок, FR-41 (`test_remd_mock.py`, `test_remd_delivery.py`); 15 — диаризация расшифровки врач/пациент, FR-42 (`test_session_diarization.py`); 25 — обоснованный черновик: диагноз и лечение по источникам КБ, FR-43 (`test_session_ai_grounding.py`); 23 — тул протокола и диалог по сессии, FR-44 (`test_session_agent.py`); 4 — маршрутизация двух тем на вопрос (FR-14); 2 — риск-паттерн чек-ина self_harm/suicidal и risk_assessed (docs/16 §2); 1 — fulltext-ссылка в библиотеке врача (FR-36); 1 — идемпотентность сида и сохранность реестра КБ при деплое (docs/03 §13), ML-сайдкар в stub-режиме (без БД/GPU/torch) 82, e2e (UI-флоу) 62 в 23 файлах.
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
| `test_risk_flags_rejects_visible_omitted_item` | HADS-D, `omitted={d1}` (видимый обратный пункт) | risk_flags() | ValueError «cannot be omitted» — тот же guard, что у `score()`, а не молчаливый NOT_ASSESSED |
| `test_risk_flags_requires_complete_answers` | PHQ-9 q1..q8 без q9 и без `omitted` | risk_flags() | `IncompleteAnswersError` — неполный набор не считается по `.get(..., 0)` |
| `test_risk_flags_uses_scored_total_with_hidden_item` | PHQ-8 15 / 14, q9 скрыт | risk_flags() | сумма из `score()`: 15 → SEVERE_SYMPTOMS + NOT_ASSESSED, 14 → только NOT_ASSESSED |
| gad7_severity_boundaries | GAD-7 total 0/5/10/15 | score() | NONE/MILD/MODERATE/SEVERE |
| triage_red_suicidal | flag SUICIDAL, severity MILD | triage() | RED (флаг важнее severity) |
| triage_red_severe | severity SEVERE, флагов нет | triage() | RED |
| triage_yellow | severity MODERATE, флагов нет | triage() | YELLOW |
| triage_green | severity NONE, флагов нет | triage() | GREEN |
| triage_deterministic_input_output_table | таблица вход/выход по границам | triage() | GREEN/YELLOW/RED по таблице |

### 2.3 checkin (адаптивный, docs/16)

| Тест | Given | When | Then |
|---|---|---|---|
| test_core_is_first_step / test_core_step_returns_only_unanswered_items | пустые ответы, затем часть ядра | plan() | шаг `core` = 7 пунктов в фиксированном порядке, возвращаются только неотвеченные |
| test_routing_is_deterministic | одинаковые ответы | plan() | одинаковая последовательность шага и модулей |
| test_clean_day_has_no_modules | все симптомы 0, SL1 = 4, HR4 = 0 | plan() | `done`, модулей нет |
| test_gate_yes_runs_safety_module_and_skips_symptoms | HR4 = 1 | plan() | шаг `safety` RS1–RS3, симптоматические модули не предъявлены |
| test_gate_yes_finishes_after_safety_module | HR4 + RS1–RS3 | plan() | `done`, safety `signal`/`review` по RS |
| test_urgent_signal | RS1/RS2 = 1 или RS3 = 0 | safety_level() | `signal` |
| test_gate_unanswered_is_unassessed_not_negative | HR4 отсутствует | safety_level() | `unassessed`, а не `none` |
| test_thresholds_are_inclusive | значения на границах правил | select_modules() | модуль включается при «≥»/«≤» включительно |
| test_limit_is_twelve_and_modules_are_whole | много симптомов | select_modules() | итог ≤ 12, модули целиком, непоместившиеся — с причиной |
| test_never_exceeds_limit_for_any_core_combination | перебор значений ядра | planned_items() | никогда > 12 |
| test_validate_answers_rejects_bad_values | нецелое/bool/вне диапазона | validate_answers() | ValueError |
| test_pss10_reverse_scoring_concrete_total | положительные пункты 4/5/7/8 | score(PSS10) | обратный подсчёт, конкретная сумма |
| test_pss10_severity_boundaries | пороги 13/14 и 26/27 | severity_for() | NONE/MILD/MODERATE без SEVERE |
| test_hdrs17_severity_boundaries / test_hdrs17_triage | HAM-D17 (8/17/24), h3 как суицидальный пункт | severity_for()/triage() | MODERATE и RED при h3≥1 |
| test_every_item_has_anchors_for_all_values | все шкалы ALL_SCALES | ScaleItem.anchors | `len(anchors) == max_value+1`, строки непустые |
| test_binary_scale_anchors_are_yes_no / test_questionnaire_frequency_anchors | HCL-32, PHQ-9, PSS-10 | anchors | HCL — («Нет», «Да»); PHQ-9 — («Совсем нет», «Несколько дней», «Более половины дней», «Почти каждый день»); PSS-10 — 5 подписей частоты |
| test_hdrs17_anchors_follow_item_max_value | HDRS-17 (пункты 0–4 и 0–2) | anchors | число якорей 5/3 по пункту, h1 — 5 разных подписей |
| test_anchors_length_mismatch_raises | anchors длиной ≠ max_value+1 | ScaleItem() | ValueError; пустые anchors допустимы |

### 2.4 trend

| Тест | Given | When | Then |
|---|---|---|---|
| trend_empty_input | runs = [] | trends() | пустой тренд, без исключений |
| trend_delta_30 | 30-дневная серия с известной разницей | trends() | delta_30 = конец − начало |
| trend_moving_avg | серия [1,2,3,4,5] окно 7 | trends() | avg = расширяющееся среднее: длина ряда та же, значения = (1/1, 3/2, 6/3, 10/4, 15/5); окно > длины ряда — не ошибка, а полное среднее |

### 2.5 деперсонализация (docs/12)

| Тест | Given | When | Then |
|---|---|---|---|
| phone_forms | 8 форм РФ-телефона (`+7…`, `8 (…)`, `9261234567`) | depersonalize() | `[ТЕЛЕФОН]`, normalize_phone → E.164 |
| document_forms | СНИЛС (с дефисами/слитно), ИНН 10/12, ОМС 16 (с пробелами/слитно), паспорт, медкарта/ИБ | depersonalize() | `[ДОКУМЕНТ]`, Finding.category точная |
| dob_forms | `12.03.1954`, `12-03-54`, `1985-04-17`, `9 августа 1949`, `… г.р.` | depersonalize() | `[ДАТА РОЖДЕНИЯ]`; дата без маркера (приём) не маскируется |
| span_overlap | частичный + полный спан ФИО + телефон | depersonalize(extra_spans=…) | один плейсхолдер на сущность, дыр нет |
| age_combo | «Петрова Анна, 64 года» с/без ФИО-спана | depersonalize() | `[ВОЗРАСТ]` только при ФИО в той же фразе |
| fio_heuristic | `сидорова галина`, `иванов пётр сергеевич`, `Иванов П.С.`, `П.С. Иванов`, `ПЕТРОВА` | depersonalize() | `[ФИО]` без deny-list и NER |
| fio_negative | «больница городская», «голова болит», «принимаю аспирин 5 дней» | depersonalize() | текст не меняется (морфология фамилии без якоря-имени) |
| phone_seven_prefix | `7 926 123-45-67`, `7(916)555-77-33`, `79261234567` | depersonalize() | `[ТЕЛЕФОН]` целиком, включая префикс 7 |
| grouped_documents | СНИЛС `123 456 789 01`, ИНН `77 07 083 229`, ОМС `1234 5678 9012 3456`, паспорт `45 09 123456` | depersonalize() | `[ДОКУМЕНТ]` с точной Finding.category |
| medcard_bare | `№ 123456`, `карта 123456`, `ИБ 456789` | depersonalize() | `[ДОКУМЕНТ]`, category=MEDCARD |
| address_street_on | «Живу в Москве на Ленина 5» | depersonalize() | `[АДРЕС]` |
| age_me | «иванов пётр, мне 34» / «мне 34» без ФИО | depersonalize() | `[ВОЗРАСТ]` только в связке; одиночный возраст не маскируется |

Полный gold-прогон: `bench/.venv/bin/python bench/ner/run_clinical_bench.py
--predict regex,hybrid` — 418 контекстов, P/R/F1 = 1.000/1.000/1.000 по каждой
категории (замер 2026-10-25, см. docs/12 и REPORT-pii.md).


| remote_mask | фейковый `/v1/mask` (без GPU) + `CC_DEPERSONALIZE_NER=remote` | scrub_text() | те же плейсхолдеры, что hybrid; `names` и `X-CC-ML-Token` доходят |
| remote_fail_closed | prod + недоступный sidecar | scrub_text() | 503 `DEPERSONALIZATION_UNAVAILABLE`, ноль вызовов LLM |
| remote_malformed_response | sidecar отдаёт строковые/вне диапазона/неизвестные спаны | scrub_text() в prod / вне prod | 503 до LLM в prod; regex-деградация вне prod (без «успешного» немаскирования) |
| deny_list_capped | deny-list на 200 ФИО | `ml_client.mask` | в сайдкар уходит 128 записей |
| partial_overlap_masks_tail | спаны 0–11 и 7–21 по «Иванов Пётр Иванович» | resolve_spans + mask_spans | хвост второго спана тоже `[ФИО]` |
| insights_scrub_before_truncate | телефон на границе 500 символов | `_dialogue_text` → prompt | в prompt нет ни полного номера, ни обрезка `+7926` |
| clinician_own_fio_masked | `CC_DEPERSONALIZE_NER=off`, ФИО врача в вопросе | `/assistant/ask` | ФИО маскируется deny-list'ом (не зависит от NER) |

## 3. Интеграционные тесты (api/tests, реальный Postgres)

| Тест | Сценарий | Ожидание |
|---|---|---|
| auth_2fa_required | регистрация CLINICIAN → login без otp | 401; с корректным TOTP → 200 |
| auth_otp_optional_flag | CC_REQUIRE_OTP=false → login CLINICIAN без otp → 200; секрет остаётся в БД; prod_mode+false → fail-fast |
| auth_config_public | публичный GET /auth/config → {require_otp} по CC_REQUIRE_OTP (UI скрывает поле 2FA) |
| auth_email_reset_request_unknown (`test_auth_email.py`, FR-48) | POST /auth/password/reset-request для несуществующего адреса → 202 `{sent:true}` | ни строки в `auth_codes`, ни `demo_code` — перечисления аккаунтов нет |
| auth_email_reset_cooldown | повторный reset-request <60 с | 429 `CODE_COOLDOWN` |
| auth_email_reset_e2e | reset-request (`demo_code`) → POST /auth/password/reset | 204 без тела; новый пароль входит, старый — 401; аудит `PASSWORD_RESET_REQUEST`/`PASSWORD_RESET` только с id |
| auth_email_reset_invalid | неверный / истёкший / использованный код / 5 промахов | 422 `CODE_INVALID` во всех случаях (причина не раскрывается) |
| auth_email_reset_kills_sessions | reset → POST /auth/refresh старым refresh-токеном | 401 (все `refresh_tokens` пользователя удалены) |
| auth_email_reset_hash | код из `demo_code` vs `auth_codes.code_hash` | в БД 64-символьный SHA-256, открытого кода нет |
| auth_email_otp_login (`CC_EMAIL_OTP=true`) | login пациента → 202 `{otp_required:"email", challenge}`; `/auth/login/otp` с неверным кодом → 422, с верным → 200 + cookies | токены только после кода; аудит `LOGIN_OTP_SENT`/`FAILED`/`OK` без e-mail |
| auth_email_otp_off | `CC_EMAIL_OTP=false` → login пациента | ответ ровно `{access_token, refresh_token, role}`, `auth_codes` не пишется |
| auth_email_prod_fail_closed | `CC_PROD_MODE=true` и SMTP не настроен → reset-request | 503 `MAIL_UNAVAILABLE`, без `demo_code` |
| auth_email_demo_code_flag | `CC_ALLOW_DEMO_SECRETS=true`, `CC_MAIL_DEMO_CODES=false`, SMTP пуст → reset-request и login пациента (`CC_EMAIL_OTP=true`) | 503 `MAIL_UNAVAILABLE` одинаково для существующего и несуществующего адреса, `demo_code` нет, `auth_codes` пуст; с флагом `true` — 202 и 6-значный `demo_code` |
| llm_router_* | LLM-роутер раздела: tool_call search_<раздел> → topic, off→None, ошибка/таймаут/неизвестная тулза/без тулзы → None (fallback все разделы) |
| `rewrite_followup_*` (FR-21) | LLM-переформулировка уточняющего вопроса треда: первая строка ответа без кавычек/префикса, no URL → None без вызова, пустая история → None без вызова, ошибка/таймаут/пустой ответ → None, обрезка истории 8×800≤3000 с конца | самодостаточный запрос либо None (= поиск по исходному вопросу), ответ не ломается |
| consent_gating | PATIENT без согласия → POST /screenings | 403 CONSENT_REQUIRED; после /consent → 201 |
| screening_closes_run | finish → повторные answers | 409 RUN_CLOSED |
| screening_one_open_only | 2-й screenings при открытом | 409 CONFLICT |
| test_questionnaires_expose_answer_anchors | GET /questionnaires | каталог шкал | у каждого пункта `anchors` длиной `max_value+1`, PHQ-9 и HCL-32 — ожидаемые подписи |
| escalation_under_1s | красный ответ → POST finish | 201; escalation создан; время полного ответа < 1 с |
| escalation_notifies | RED с суицид. флагом | уведомление врачу в БД (escalations.row) + контакты помощи в ответе |
| checkin_start_core_seven | POST /checkins (docs/16 §4) | `201`, status in_progress, шаг `core` из 7 пунктов, `progress {0,7}` |
| checkin_start_idempotent | повторный POST /checkins в тот же день | `200`, тот же `session_id`, одна строка `checkin_sessions`, один `CHECKIN_START` в аудите |
| checkin_once_per_day | POST /checkins и answers после завершения дня | `409 CHECKIN_ALREADY_DONE` |
| roster_enforcement | врач без roster → GET patients/{id} | 403 NO_ROSTER; после /admin/roster → 200 |
| test_scales_catalog_exposes_anchors | GET /clinician/scales | каталог шкал | `scales[].items[].anchors` длиной `max_value+1`, HDRS-17: h1 — 5 якорей, h4 — 3 (каталог больше не едет в `report`) |
| patient_cannot_read_others | PATIENT → GET diary другого пациента / report | 403 |
| diary_mood_scale | POST /diary с mood 0 и 5 | 0 → 201 (5-точечная шкала), 5 → 422 VALIDATION |
| audit_append_only | прямой UPDATE/DELETE audit_log на уровне БД | ошибка БД-триггера (исключение) — отдельный БД-тест, вне HTTP-слоя (API не предоставляет UPDATE/DELETE-эндпоинтов для audit_log) |
| audit_truncate_blocked | прямой TRUNCATE audit_log на уровне БД | ошибка БД-триггера (statement-уровень `trg_audit_no_truncate`) |
| audit_records_access | чтение report | в audit_log появилась запись (actor, action='READ_REPORT') |
| consent_revoke | revoke → повторное обращение к медданным | 403 CONSENT_REQUIRED |
| questionnaire_catalog | GET /questionnaires (FR-2) | каталог шкал PHQ-9/GAD-7/HADS-A/HADS-D из core; тексты не дублируются в UI |
| checkin_partial_step | отправлена часть пунктов текущего шага | `422 CHECKIN_INCOMPLETE` |
| checkin_unknown_item | неизвестный пункт или пункт другого шага | `422 CHECKIN_UNKNOWN_ITEM` |
| checkin_out_of_range | 5 / −1 / `true` / `"2"` / 1.5 / null, HR4=2 | `422 CHECKIN_OUT_OF_RANGE` (не 500 и не VALIDATION) |
| checkin_module_routing | MO1 ≥ 3 в шаге ядра | шаг `modules` = HR1–HR3 + MO2–MO3, причины модулей в ответе |
| checkin_limit_12 | MO1=4, SL1=0 (много модулей) | предъявлено ≤ 12 пунктов, модули целиком, пропущенные — с причиной «лимит 12» |
| checkin_gate_routes_safety | HR4=«Да» | шаг `safety` RS1–RS3, `planned 10`, симптоматические модули не предъявлены |
| checkin_risk_signal_escalates | HR4=«Да» + RS1=«Да», врач в roster | одна `Escalation` на сеанс: `CHECKIN_RISK` сразу после HR4 → `CHECKIN_RISK_URGENT` на `signal`, два `Notification` ростерному врачу, `notified_clinician_at` заполнен, `clinician_notified=true`, аудит `CHECKIN_RISK_SIGNAL` |
| checkin_risk_no_roster | HR4=«Да» + RS2=«Да», ростера нет | `Escalation CHECKIN_RISK_URGENT` создана, `notified_clinician_at = NULL`, уведомлений нет, `clinician_notified=false` |
| checkin_gate_open_signal | HR4=«Да», RS не отправлены | эскалация уже есть, `status=in_progress`, `phase=safety`, уведомление ростерному врачу создано |
| checkin_review_state | HR4=«Да», RS1/RS2=«Нет», RS3=«Да» | `safety=review`, одна `Escalation CHECKIN_RISK`, повторного уведомления нет |
| checkin_no_duplicate_escalation | повторная отправка ответов после завершения | `409`, эскалация одна (partial unique по `checkin_session_id`), уведомлений по-прежнему два |
| checkin_duplicate_step_submit | повторный submit шага ядра с HR4=«Да» | `422 CHECKIN_UNKNOWN_ITEM`, одна эскалация и одно уведомление |
| checkin_ensure_escalation_idempotent | прямой `_ensure_escalation` (urgent=False, снова False, затем True) | одна строка; `kind` повышается до `CHECKIN_RISK_URGENT`; второе уведомление только при повышении |
| escalations_queue_access | открытая эскалация; врач, админ, пациент | врач/админ — `200` (по умолчанию `status=open`) с `{id, patient_id, patient_fio, kind, safety, checkin_session_id, notified_at, created_at, resolved_at, resolution, in_roster, risk_answers}`; у открытой `resolved_at`/`resolution` — `null`; пациент — `403`; закрытая (`resolved_at`) в `open` не попадает |
| escalations_queue_roster | один пациент в ростере врача, второй — нет | `in_roster=true` только у своего врача; у админа всегда `false`; `risk_answers` содержит только `HR4, RS1, RS2, RS3` |
| escalations_queue_window_limit | эскалация с `created_at` 91 день назад; `?limit=1`; `?limit=201` | старая не попадает (окно 90 дней), `limit=1` → один элемент, `limit=201` → `422 VALIDATION` |
| escalations_resolve | врач закрывает сигнал `{resolution}`; повтор; чужой id; пациент | `200 {id, resolved_at, resolution}`, аудит `ESCALATION_RESOLVE`, после закрытия очередь пуста; повтор → `409`, неизвестный id → `404`, пациент → `403` |
| escalations_queue_status_filter | открытая + закрытая эскалации; `?status=resolved`, `?status=all`, `?status=bogus` | `resolved` — только закрытые, свежие по `resolved_at` сверху, с `resolution`; `all` — оба вида; иной статус → `422 VALIDATION`; аудит `READ_ESCALATIONS` не меняется |
| escalations_resolve_bad_id | `POST /escalations/not-a-uuid/resolve` | `404 ESCALATION_NOT_FOUND` (а не `422 VALIDATION`); реальная эскалация остаётся открытой |
| escalations_resolution_length | `resolution` 1001 и 1000 символов | 1001 → `422 RESOLUTION_TOO_LONG` (не `VALIDATION`), сигнал не закрыт; 1000 → `200`, комментарий сохранён целиком |
| escalations_resolution_encrypted | raw `SELECT resolution::text` | bytea (`\x…`), текста в открытом виде нет; ORM расшифровывает обратно |
| consent_revoke_checkin | завершённый риск-чек-ин + эскалация + уведомление | после отзыва согласия нет сеансов, эскалаций и уведомлений |
| diary_mood_scale | новая запись `mood=0`; историчная строка `mood=7, mood_scale=NULL` | `mood_scale="0-4"` / `"1-10"` в `/diary` и в карте врача; старые строки не переписываются; `mood=5` → `422` |
| checkin_foreign_session | чужой или несуществующий `session_id` | `404 CHECKIN_NOT_FOUND` |
| checkin_answers_encrypted | raw `SELECT answers::text` из БД | bytea (`\x…`), идентификаторы пунктов в открытом виде отсутствуют; ORM расшифровывает обратно |
| checkin_presented_steps | после шага ядра | `presented` хранит шаги, предъявленные пункты и основания модулей |
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
| `test_compose_vapid_key_is_valid_p256`, `test_vapid_public_key_matches_private`, `test_invalid_vapid_key_raises` | разбор дефолтного VAPID-ключа из `infra/docker-compose.yml`; ключ из env; мусорный ключ | дефолтный ключ — валидный EC P-256; публичный ключ выводится (87 симв., uncompressed point); невалидный → `ValueError` (422 на `/push/vapid-public-key`) |
| `test_push_to_clinicians_signs_request` | подписка врача на loopback-push-сервер; `push_to_clinicians` | запрос ушёл, `Authorization: vapid t=…`, `Content-Encoding: aes128gcm`, тело зашифровано (на старом коде запросов 0) |
| fcm_upgrade_then_cooldown | POST /push/fcm/upgrade дважды | первый 200 `{upgraded}`; второй 429 `FCM_COOLDOWN`; /push/fcm/status окно активно |
| `test_guideline_with_confirmed_license_is_auto_published` / `…without_license_waits_for_human` | гайдлайн с CC BY-NC-SA в тексте и без лицензии | авто `approved` только при подтверждённой лицензии; иначе `candidate` |
| `test_article_is_candidate_until_approved`, `test_article_without_psychiatry_is_excluded` | статья PubMed проходит/не проходит жёсткие критерии | `candidate` до approve; непсихиатрическая тема → `excluded`, в индексе нет |
| `test_proprietary_guideline_cannot_be_approved`, `test_retracted_article_cannot_be_approved` | `All rights reserved`; ретракция | approve → `ReviewError`/409, точек в Qdrant 0 |
| `test_rerun_is_idempotent`, `test_rerun_keeps_approved_article_and_its_chunks`, `test_new_article_version_requires_new_approval` | повторные прогоны и новая версия | unchanged не переиндексируется, решение человека сохраняется, новая версия → `candidate` |
| `test_reindex_after_external_collection_reset` | коллекция Qdrant wiped | сверка `remote_count` восстанавливает approved-контент |
| `test_articles_index_has_provenance_and_abstract_only` | payload статьи | каждый чанк несёт PMID/DOI/URL; полный текст статьи не хранится |
| `test_fetch_documents_rejects_mismatched_handle`, `test_offline_mode_never_calls_network` | подмена ответа IRIS; `CC_ENRICH_OFFLINE` | SourceError; сеть не вызывается |
| `test_admin_approve_publishes_to_qdrant`, `test_admin_reject_requires_reason_and_records_it` | approve/reject через API | индексация, причина reject, аудит, 409/422 |
| `test_clinician_can_hide_and_restore_source`, `test_assistant_retrieval_respects_personal_exclusion` | персональное исключение | источник скрыт только у этого врача, в обоих путях retrieval |
| `test_commit_failure_never_publishes_uncommitted_content` | сбой commit реестра во время прогона | ни одной точки в Qdrant, `failed_documents=1`, записи `kb_works` нет |
| `test_failed_reindex_restores_previous_points` | upsert новой версии падает после delete | опубликованная версия восстановлена (точки и payload прежние) |
| `test_excluded_work_cannot_be_approved` | `excluded`-работа | `ReviewError`: approve допустим только для `candidate` |
| `test_request_rejects_untrusted_url_before_network`, `test_redirect_hop_is_validated_like_the_first_request` | URL из JSON IRIS: `http:`, `file:`, link-local/metadata, `localhost`, чужой хост; cross-host редирект | `SourceError` до сетевого вызова; каждый редирект проверяется так же |
| `test_detect_license_scans_whole_document` | строка лицензии на ~50 000-м символе документа | `CC BY-NC-SA 3.0 IGO` → `met` (раньше документ отвергался) |
| `test_parse_rejects_dtd_before_parsing`, `test_request_caps_body_size` | XML с `DOCTYPE`/`ENTITY`; ответ PubMed > 8 MiB | `SourceError` до разбора; тело сверх лимита не принимается |
| `test_reindex_republishes_only_approved`, `test_reindex_failure_does_not_abort_the_rest` | `reindex` после сброса коллекции Qdrant (фейковый `index`) | публикуются только `approved`; сбой одной работы не отменяет остальные; итог `переиндексировано N, пропущено M, ошибок K` |
| `test_reindex_skips_works_with_stale_criteria_version` | работа `approved` с `criteria.version=0.9.0` | не публикуется, в stderr «критерии устарели», нужен повторный `run` |
| `test_fetch_documents_labels_non_english_stream` | выбран русский TEXT bitstream IRIS | документ получает `language="ru"`, а не `en` |
| `test_thread_create_message_and_read_back` | тред с 2 пациентами → вопрос → чтение | оба сообщения и цитаты в истории, карты обоих пациентов в ответе, дедуп `patient_ids` |
| `test_foreign_thread_is_404` | чужой тред: GET/PATCH/POST/DELETE | 404 `THREAD_NOT_FOUND`, чужой тред не виден в списке |
| `test_patient_ids_roster_gate` | пациент вне roster в `patient_ids` | 403 `PATIENT_NOT_IN_ROSTER` (создание и сообщение) |
| `test_patient_limit` | >10 `patient_ids` | 422 `TOO_MANY_PATIENTS` |
| `test_read_thread_with_patients_is_audited` | чтение треда с пациентами | запись `READ_ASSISTANT_THREAD` в audit_log |
| `test_delete_thread_cascades` | DELETE треда | 404 после удаления, сообщения и привязки удалены каскадом |
| `test_redline_in_history_does_not_poison_next_turn` | ход 1 — redline (диагноз), ход 2 — обычный вопрос по КБ | ход 2 не refused и возвращает KB-цитаты (red line только по текущему вопросу) |
| `test_revoked_consent_skips_patient_card` | отзыв согласия после создания треда | `skipped_patient_ids == [p1]`, карты p1 нет в цитатах, запись `READ_ASSISTANT_THREAD` |
| `test_followup_question_rewritten_for_retrieval` | тред: «что известно про Авиандр?» → «а в каких дозах?» | LLM-переформулировка уходит в агент как `retrieval_query`, исходный вопрос — как `query` (red line/ответ); первый ход LLM не вызывает; `rewritten=true` |
| `test_followup_without_llm_uses_current_question`, `test_followup_llm_failure_falls_back_to_original` (пустой ответ/URLError/таймаут) | LLM выключен или недоступен | поиск по исходному вопросу, `rewritten=false`, ответ не ломается |
| `test_followup_rewrite_prompt_has_no_pii` | ФИО/телефон/дата рождения в прошлой реплике | в промпте переформулировки только `[ФИО]/[ТЕЛЕФОН]/[ДАТА РОЖДЕНИЯ]`, секретов нет |
| `test_followup_redline_uses_only_current_question` | redline в истории + обычный вопрос / обычная история + redline-вопрос | red line только по текущему вопросу: поиск не глушится, отказ сохраняется |
| `test_followup_question_retrieves_drug_chunk` | «а в каких дозах?» после вопроса про препарат | чанк ЛЕКАРСТВА (`drug-aviandr`), `grounded=true` — не NO_KB_DATA |
| `test_emias_mock_*` (token/bad creds/rate limit/scopes/pagination/bundle/404) | контракт мока ЕМИАС | 401 `invalid_client`, 429 + `Retry-After`, 403 `insufficient_scope`, Bundle(searchset) с total/link[next] |
| `test_create_list_and_audit`, `test_update_fields_and_audit`, `test_stop_sets_today_and_status`, `test_delete_local_row` (FR-23) | назначение → список → правка → остановка → удаление | 201/200/204, `status='stopped'` + `ended_on` сегодня, аудит `PRESCRIBE/UPDATE/STOP/DELETE_MEDICATION` |
| `test_validation_rejects_bad_status_and_dates` | статус `unknown`, пустой title, старт в будущем, конец раньше старта | 422 `VALIDATION` |
| `test_emias_rows_read_only` | строка `source='emias'` в `patient_medications` | PATCH/stop/DELETE → 409 `MEDICATION_IMPORTED`, строка не изменилась |
| `test_roster_idor_and_other_clinician` | врач вне roster, чужой patient_id, второй врач в roster | 403 `NO_ROSTER`, 404 `NOT_FOUND`, 403 `NOT_OWNER` |
| `test_medication_encrypted_at_rest` | raw SELECT title/notes | bytea без открытого текста, ORM расшифровывает |
| `test_consent_revocation_clears_medications` | отзыв согласия пациентом | строки `patient_medications` удалены, врач получает 403 `CONSENT_REQUIRED` |
| `test_history_merges_all_types_newest_first`, `test_history_types_filter` | приём + назначение + скрининг + НЯ; `types=medication` | лента по убыванию `at`, все 4 типа и `source`, фильтр оставляет один тип, неизвестный тип → 422 |
| `test_history_limit_and_cursor` | `limit=2`, затем `before=next_before` | вторая страница без пересечения, курсор сортирует по (at, id) |
| `test_history_cursor_handles_identical_timestamps` | 3 приёма с одинаковым `visited_at`, `limit=1` | три страницы отдают все 3 строки ровно по одному разу |
| `test_history_types_deduped` | `types=medication,medication`; `types=visit,visit,lab` | дубликаты типов схлопываются (элементы не дублируются), неизвестный тип → 422 |
| `test_patient_summary_excludes_local_medications` | локальное назначение врача рядом с импортом ЕМИАС | витрина «ЕМИАС» отдаёт только `source='emias'` |
| `test_history_audit_and_gating` | чтение ленты; врач вне roster; отзыв согласия | `READ_PATIENT_HISTORY` в audit_log; 403 `NO_ROSTER`; 403 `CONSENT_REQUIRED` |
| `test_simulate_error_injection` | `?simulate=500\|429\|timeout` | 500/429(+Retry-After)/504 — клиент обязан ретраить |
| `test_emias_client_*` | кэш токена (80% TTL), 429/500, max attempts, кап 4 MiB, redirect/host allowlist, невалидный Bundle | один token-запрос на TTL; backoff/Retry-After; `response_too_large`/`host_not_allowed`/`invalid_bundle` |
| `test_no_phi_or_token_in_logs` | ретрай с СНИЛС в query | в логах только путь без query; токен и СНИЛС отсутствуют |
| `test_emias_mapping_*` | чистый маппинг Encounter/MedicationRequest/СНИЛС | типы/длительность/статусы; `sha256(СНИЛС+перец)` детерминирован |
| `test_sync_patients_creates_then_is_idempotent` | два прогона подряд | 1-й: created=5; 2-й: created=0, skipped≥5, дублей нет |
| `test_savepoint_isolation_keeps_good_entities` | битый Encounter среди валидных | errors=1, `partial`, остальные сущности сохранены |
| `test_duplicate_emias_id_updates_instead_of_creating` | два entry с одним emias_id | одна строка visits, второе — update |
| `test_emias_admin_*` | RBAC/503 без флага/журнал/аудит | 403 для не-ADMIN; `503 EMIAS_DISABLED`; `EMIA_SYNC` в audit_log |
| `test_clinician_emias_summary_requires_roster` | чтение блока «ЕМИАС» без roster | 403 NO_ROSTER; с roster → 200 и аудит `READ_EMIAS` |
| `test_clinician_sets_and_reads_contacts` | врач PATCH/GET контактов roster-пациента | `phone` нормализован в `+7XXXXXXXXXX`, `birth_date`/`age`/`adult` в ответе |
| `test_phone_normalization` / `test_phone_invalid` | `+7…`/`8…`/`7…`/10 цифр; 5 цифр, `+1…`, буквы | все варианты → `+79261234567`; невалидные → 422 `PHONE_INVALID` |
| `test_birth_date_bounds_and_minor_flag` | будущая дата, 1899, `DD.MM.YYYY`; дата 10 лет назад | 422 `BIRTH_DATE_INVALID`; несовершеннолетний сохранён с `adult:false` |
| `test_patient_list_and_card_mask_phone` | контакты заданы; GET списка и report | в строке и карте телефон маской `+7•••123-45-67`, прежние ключи не сломаны |
| `test_rbac_contacts` | пациент → clinician-endpoint; врач вне roster; врач → `/profile/contacts` | 403; 403 `NO_ROSTER`; 403 |
| `test_audit_rows_written` | чтение/правка контактов врачом и пациентом | `READ_*`/`UPDATE_*_CONTACTS` в audit_log без ПДн в payload |
| `test_patient_self_update_only_own` | два пациента, PATCH своего профиля | чужой `users`-ряд не изменён; маршрута с чужим id нет |
| `test_consent_revocation_clears_contacts` | контакты заданы → POST /consent/revoke | `users.phone`/`birth_date` = NULL; контакты недоступны (403 `CONSENT_REQUIRED`) |
| `test_create_and_list_with_due_states` | напоминания с разными сроками; GET с `due=` | `due_state` overdue/today/soon/later; «сегодня» — день МСК; `overdue`-счётчик для бейджа |
| `test_unrostered_patient_forbidden` | создание по пациенту вне roster; GET его напоминаний | 403 `NO_ROSTER` в обоих случаях |
| `test_idor_other_clinician_cannot_see_or_touch` | PATCH/done/cancel чужого напоминания, список другого врача | 404 на чужое (не подтверждаем существование), свой список пуст; пациенту — 403 |
| `test_patch_and_transitions` | PATCH note/kind/due_at; done → done; cancel; повтор | значения применены; `status`/`done_at` выставлены; повтор → 409 `REMINDER_CLOSED` |
| `test_note_encrypted_at_rest` | note с ФИО/состоянием | в БД bytea без plaintext; через API текст расшифрован |
| `test_patients_sorted_red_first_without_assessment_last` (FR-30) | список карточек: GREEN/YELLOW/RED/без оценки, фильтр `?triage=` | порядок RED→YELLOW→GREEN→без оценки; фильтр возвращает только RED |
| `test_patients_sorted_by_escalations_then_freshness` (FR-30) | два GREEN-пациента: открытая эскалация, затем состаренная оценка | выше тот, у кого больше открытых эскалаций; при равенстве — свежая оценка |
| `test_clinician_chats_only_roster_sorted_with_preview` (FR-29) | `GET /clinician/chats` для двух roster-пациентов и одного вне roster | только roster; `last_at` desc; `preview` ≤120 с «…», дешифрован сервером; `message_count`/`last_sender` |
| `test_clinician_chats_scoped_to_clinician_and_role` (FR-29) | второй врач и пациент | чужой врач видит пустой список; пациенту — 403 `ROLE_MISMATCH` |
| `clinician-admin.spec.ts`, `chats.spec.ts`, `escalations-queue.spec.ts` (FR-32) | врач после входа | кабинет с приветствием/напоминаниями/очередью; левое меню открывается и ведёт в «Пациенты», «Ассистент», «Чаты с пациентами», «Расписание», «Напоминания», «Библиотеку»; фильтры называются «Риск» |
| `clinician-assessment.spec.ts`, `clinician-visit.spec.ts`, `clinician-adverse-event.spec.ts`, `clinician-links.spec.ts` (FR-32) | карточка пациента | сверху риск и алёрт при RED; HDRS-17 внутри «Назначить скрининг»; «Приёмы»/НЯ доступны; deep-link `#patient=` работает; «Динамика»/«История скринингов» свёрнуты |
| `clinician-schedule.spec.ts` (FR-38/39) | врач после входа → «Расписание» | запись создаётся формой и видна в списке дня; пересечение — 409 `SLOT_CONFLICT` строкой; «Начать приём» открывает панель; пустой черновик не подтверждается (409 `PROTOCOL_NOT_READY` со списком разделов); после заполнения §5.1 и approve протокол read-only и переживает перезагрузку; приём без записи находится через «Приёмы без записи» после закрытия панели и reload; запись правится (PATCH) и отменяется |
| `test_analyze_creates_pending_encrypted_and_dedupes` (FR-31) | fake `/chat/completions` вернул 2 факта; повторный `POST .../chat/analyze` | оба факта `pending`, `text` в БД — bytea, `source_message_ids` заполнены; повтор: `created=0`, `skipped=2`, строк по-прежнему 2 |
| `test_prompt_masks_fio_and_limits` (FR-31) | в переписке ФИО пациента и врача, телефон | в теле запроса к LLM нет ФИО/телефона; есть `[ФИО]`; промпт ограничивает 10 фактов и 200 символов |
| `test_disabled_returns_503_without_llm_call` (FR-31) | `CC_CHAT_INSIGHTS_ENABLED=false`, URL задан | `503 CHAT_INSIGHTS_DISABLED` (detail ссылается на docs/17); `urlopen` не вызван; list — тоже 503 |
| `test_llm_error_and_garbage_write_nothing` (FR-31) | LLM бросает `URLError`/возвращает не-JSON | `503 CHAT_INSIGHTS_UNAVAILABLE`, в `chat_insights` 0 строк |
| `test_empty_dialogue_409` (FR-31) | roster-пациент без сообщений | `409 CHAT_INSIGHTS_EMPTY`, LLM не вызвана |
| `test_accept_in_report_reject_not_and_404` (FR-31) | accept одного, reject другого, повтор accept | accepted виден в `report.chat_insights` без `source_message_ids`; rejected — нет; повтор идемпотентен; неизвестный id → `404 INSIGHT_NOT_FOUND` |
| `test_foreign_patient_and_insight_404` (FR-31) | второй врач вне ростера; факт чужого пациента через свой ростер | `403 NO_ROSTER`; `404 INSIGHT_NOT_FOUND` |
| `test_patient_role_forbidden` (FR-31) | пациент на analyze/list/decide | `403 ROLE_MISMATCH` |
| `test_consent_revoke_deletes_insights` (FR-31) | анализ → `POST /consent/revoke` | строк `chat_insights` пациента нет |
| `test_worker_tick_creates_one_notification_idempotent` | два тика `deliver_due` по одному due-напоминанию | ровно один `Notification kind='REMINDER'`; `notified_at` заполнен; второй тик — 0 |
| `test_worker_respects_advisory_lock` | лок удерживает другой коннект | тик возвращает 0 и не создаёт уведомление; после снятия лока — доставка |
| `test_consent_revocation_destroys_reminders` | напоминание задано → POST /consent/revoke | строк пациента в `clinician_reminders` нет |
| `test_percentage_from_expected_doses` (`test_adherence.py`) | режим 2×день, назначение 7 дней назад, 5 отметок `taken` | `expected=14`, `score=5`, `pct=36` (а не 5/5 = 100%) |
| `test_missing_start_date_counts_from_creation` (`test_adherence.py`) | назначение без `started_on` создано сегодня, режим 2×день, 1 `taken` | `expected=2` (с даты создания, не за всё окно), `pct=50` |
| `test_onset_date_is_validated_by_moscow_calendar` (`test_adverse_events.py`) | граница «сегодня» подменена на 24.10 МСК | 24.10 → 201, 25.10 → 422 `VALIDATION` |
| `test_expected_is_calendar_days_not_weekdays` | тот же режим, окна 7 и 14 дней | знаменатель — все календарные дни активности; выходные не исключаются |
| `test_ended_on_cuts_expected_days` | `started_on` = −9 дн, `ended_on` = −3 дн, окно 7 дней | `active_days=4`, `expected=4`, 2 `taken` → 50% |
| `test_overall_pct_is_weighted_not_average` | препарат A 2/2, препарат B 0/1 | общий 67% (2/3), не среднее 50% |
| `test_partial_counts_half` | 3 `partial` без `partial_kind` (legacy) при режиме 2×день | `partial_kind=less_than_prescribed`, `score=1.5`, `pct=75` |
| `test_partial_kind_contributes_its_share` (7 кодов) | по одной `partial`-отметке на режим 1×день | `score` = 0,25 / 0,5 / 0,75 / 0,5 / 0,667 / 0,5 / 0,5; `pct` = 25/50/75/50/67/50/50 |
| `test_not_scheduled_removes_expected_doses_of_that_day` | `taken` + `not_scheduled` при режиме 2×день и 7 днях | `expected=12`, `score=1`, `pct=8`, `not_scheduled=1`, `missed=0` |
| `test_all_days_not_scheduled_leaves_no_denominator` | все дни «не назначено» | `expected=0`, `pct=null` |
| `test_legacy_partial_without_kind_still_accepted` | старый клиент: `partial` без поля `partial_kind` | 201, `partial_kind=less_than_prescribed`, доля 0,5 |
| `test_partial_with_explicit_null_kind_422` | `partial` + явный `partial_kind: null` | 422 `PARTIAL_KIND_REQUIRED` |
| `test_partial_kind_rejected_for_other_statuses` | `taken`/`missed`/`not_scheduled` + `partial_kind=half` | 422 `PARTIAL_KIND_INVALID` |
| `test_unknown_partial_kind_422` | `partial` + `partial_kind=most` | 422 (валидация схемы) |
| `test_no_schedule_means_no_percent` | `times_per_day IS NULL`, отметка привязана | `pct=null`, `expected=0`, отметка в `excluded` |
| `test_no_medications_gives_empty_aggregate` | нет назначений | `medications=[]`, `summary.pct=null` |
| `test_legacy_records_are_outside_denominator` | отметка без `medication_id` рядом со связанной | 100% по назначению, legacy-отметка только в `excluded`/`total` |
| `test_post_foreign_or_stopped_medication_422` | `POST /me/adherence` с чужим и остановленным назначением | 422 `MEDICATION_NOT_ACTIVE` |
| `test_times_per_day_validation` | создание/правка `times_per_day` = 0 и 7; затем 4 | 422 `TIMES_PER_DAY_INVALID`; 4 сохраняется |
| `test_consent_revoke_deletes_linked_records_and_medications` | отметка по назначению → POST /consent/revoke | `adherence_records` и `patient_medications` пациента пусты |
| `test_adherence_patient_records_and_doctor_aggregate` (`test_clinician.py`, обновлён) | taken + partial по назначению 2×день + legacy-отметка | `pct=75`, `expected=2`, `excluded_count=1`, агрегат в `report.adherence` |
| `test_patient_reports_pending_event` | пациент отправляет НЯ с назначением | `201`, `status=pending`, `status_label=на проверке`, `reported_by=patient`, `clinician_id`/`verified_by` пусты; событие видно в `GET /me/adverse-events` |
| `test_patient_report_requires_consent` | без активного согласия | 403 `CONSENT_REQUIRED` |
| `test_patient_foreign_medication_rejected` | `medication_id` чужого пациента, затем своё | 422 `MEDICATION_NOT_FOUND`; своё — 201 с `medication_title` |
| `test_patient_report_notifies_roster_without_pdn` | любое сообщение пациента при roster-враче | один `Notification` с `kind=ADVERSE_EVENT`, текст «Пациент сообщил о нежелательном явлении», без имени и описания; `severity: null` |
| `test_patient_cannot_set_severity` | пациент присылает `severity: severe` | поле не принимается: в ответе и в БД `null`, пуш уходит всё равно (тяжесть — колонка врача) |
| `test_verify_confirm_requires_severity` | PATCH confirm без `severity` | 422 `SEVERITY_REQUIRED`, событие остаётся `pending` |
| `test_clinician_queue_contains_pending` | `GET /clinician/adverse-events?status=pending` | событие с полями пациента/статусом, `reference=null` без препарата |
| `test_verify_confirm_sets_verifier_and_drug_reference` | PATCH confirm с `drug_id` и заметкой | `verified_by/at`, `reference.trade_name`, пациент видит «проверено врачом» без заметки; очередь pending пустеет; повтор того же статуса — 200 без изменения `verified_at`; смена на rejected — 409 `ADVERSE_ALREADY_VERIFIED` |
| `test_verify_reject` | PATCH rejected | статус `rejected`, пациент видит «отклонено» |
| `test_verify_foreign_patient_and_event_404` | врач вне ростера / чужой `event_id` | 404 `ADVERSE_NOT_FOUND` в обоих случаях |
| `test_verify_unknown_drug_404` | `drug_id` вне справочника | 404 `DRUG_NOT_FOUND` |
| `test_legacy_clinician_event_is_confirmed` | легаси POST врача | сразу `confirmed`/`clinician`, список отдаёт `status` и `reference` |
| `test_patient_report_audit_has_no_content` | создание НЯ пациентом | в `audit_log` только `ADVERSE_REPORT` и id объекта, без текста |
| `test_consent_revoke_deletes_patient_events` | НЯ → отзыв согласия | строки пациента в `adverse_events` удалены |
| `test_adverse_text_encrypted_at_rest` | после проверки врача | `event_text`/`verification_note` — `bytea`, plaintext на диске отсутствует, ORM расшифровывает |
| `test_adverse_lists_batch_references` | очередь врача и `/me/adverse-events` на 2 и на 10 событиях с назначениями | число SQL не растёт (2 не 2·N; на родительском коде тест падает), названия назначений и ГРЛС — верные |
| `test_adverse_lists_honour_limit` | `?limit=0`, `?limit=201` и `?limit=2` | 422 на границы (1..200), выдача усечена до `limit`; без параметра — все строки |
| `test_index_migration_roundtrip` (`test_migrations_indexes.py`) | alembic upgrade → downgrade -1 → upgrade на реальном Postgres | композитные индексы появляются/исчезают, одноколоночные дубли удалены, единственный head |
| `test_drugs_list_returns_whole_library` (`test_drugs.py`): справочник из `drugs.json` | GET `/drugs` | 3 препарата (Авиандр, Золофт, Эсциталопрам Канон); у каждого заполнены показания/дозы/противопоказания, `ЛП-` в РУ и непустой `source_urls` |
| `test_drugs_filter_by_query`: `q=маритупир / сертралин / эсциталопрам / ЗОЛОФТ` | GET `/drugs?q=` | находит ровно один препарат по МНН или торговому названию; несовпадающий фильтр — пусто |
| `test_drug_detail_and_404`: `/drugs/{drug_id}` | карточка препарата | поля и `source_urls`; 404 на неизвестный `drug_id` |
| `test_seed_drugs_is_idempotent`: повторный `_seed_drugs` | два прогона подряд | 3 строки, `id` не изменились — upsert по `drug_id`, не DELETE+INSERT |
| `test_kb_search_finds_new_drugs` (`test_assistant.py`): «сертралин дозировка», «эсциталопрам противопоказания» | `kb.hybrid_search` | в топе чанки `drug-zoloft*` / `drug-escitalopram-canon-safety` с `topic=ЛЕКАРСТВА` |
| `test_search_on_absent_collection_returns_empty_not_500` (`test_kb_index.py`) | коллекции в Qdrant нет (`--reset`, до `kb_seed`) | `kb.hybrid_search` (hash и dense-only) | Qdrant отвечает 404 → выдача `[]` и WARNING; ассистент уходит в keyword-fallback, а не в 500 |


### 3.1.1 Пациентский кабинет и FR-34 (`test_patient_ux.py`)

| Тест | Given | When | Then |
|---|---|---|---|
| `test_catalog_hides_suicidal_item` | шкала PHQ-9 | `GET /questionnaires` | у PHQ-9 ровно 8 пунктов `q1..q8`, текста про «умереть» нет |
| `test_screening_without_item9_scores_phq8` | ответы `q1..q8 = 0` | `finish` | `total_score=0`, GREEN, `omitted_items=["q9"]`, в `risk_flags` только `NOT_ASSESSED` (не `SUICIDAL_IDEATION`) |
| `test_screening_without_item9_can_be_red` | `q1..q8 = 3` | `finish` | `total_score=24` (максимум неполной шкалы), SEVERE → RED, эскалация создана |
| `test_legacy_client_item9_answer_still_scored` | старый клиент присылает `q9=2` | `finish` | балл учитывается, `SUICIDAL_IDEATION` → RED (явно сообщённый риск не теряем) |
| `test_chat_auto_reply_once_per_moscow_day` | два сообщения подряд | `POST /chat/thread` ×2 | первый ответ `FAQ`, второй `reply=null`; в треде 3 сообщения и 1 `SYSTEM`; перенос SYSTEM-строки на вчера снова разрешает ответ |
| `test_chat_hotline_copy_is_supportive` | RED-скрининг | `POST /chat/thread` | ответ `HOTLINE` без «специалисты уведомлены» / «требуется внимание» / обещания мгновенного контакта |
| `test_my_drug_reference_contains_only_own_medications` | два своих назначения: точное название ГРЛС и «название + дозировка» | `GET /me/drugs` | карточка только у точного совпадения (подстрочный матч не подставляет похожий препарат); аудит `READ_MY_DRUGS` без названий |
| `test_checkin_history_aggregates_without_raw_answers` | завершённый сеанс с ответами ядра | `GET /checkins/history?days=7` | `{date, wellbeing, safety}`, `wellbeing` — среднее 0..4, сырых ответов в payload нет |
| `test_checkin_history_returns_aggregate_without_raw_answers` (`test_patient_ux.py`) | пустая история | `GET /checkins/history` | `{items: []}` + аудит `READ_CHECKIN_HISTORY` (значений нет) |
| `test_questionnaire_catalog_exposes_scale_maxima` | пациентский каталог | `GET /questionnaires` | `max_total` = сумма `max_value` пунктов: PHQ-9 24, GAD-7 21, HADS 21, HCL-32 32, PSS-10 40 |
| `test_timeline_exposes_scale_max_total` | завершённый PHQ-9 | `GET /me/timeline` | у run `max_total=24` — график берёт верх шкалы с сервера, а не по коду шкалы |
| `test_prescribed_scale_completion_hides_banner` | назначена PHQ-9 | `GET /me/prescribed-scales` до и после finish | `completed_at: null` → ISO-время: баннер «Назначен скрининг» гаснет после прохождения |
| `test_crisis_reply_ignores_daily_quota` | утром израсходован FAQ, затем RED-оценка | второй POST в чат | `reply.kind=HOTLINE` с `help_resources` (112, 8-800-2000-122); лимит режет только не-кризисный FAQ |
| `test_concurrent_chat_posts_yield_one_auto_reply` | два параллельных POST в двух async-сессиях | `chat.post_message` | ровно один автоответ: `pg_advisory_xact_lock` на (пациент, сутки МСК) |
| `test_contacts_include_fio_and_email` | пациент с ФИО | `GET /profile/contacts` | в ответе `fio` и `email` (только self-service) |

`test_adverse_events.py::test_patient_report_has_no_severity_until_clinician_verifies`: пациент шлёт НЯ без `severity` → 201 и `severity: null` в БД, врач `PATCH` с обязательной `severity=moderate` подтверждает (без неё — 422 `SEVERITY_REQUIRED`, FR-34). `test_screening.py::test_finish_incomplete_422`: неполный набор (7 из 8 пунктов) → 422 `VALIDATION`.

### 3.1.2 Клинические сессии и протокол приёма (FR-38/39, `test_sessions_*.py`)

| Тест | Given | When | Then |
|---|---|---|---|
| `test_calendar_requires_clinician_role` | пациент с токеном | `GET /clinician/appointments` | 403 `ROLE_MISMATCH` |
| `test_roster_gate_and_consent` | пациент вне roster / пациент без согласия | календарь и `POST /appointments` | 403 `NO_ROSTER` / 403 `CONSENT_REQUIRED` |
| `test_slot_conflict_and_cancel_frees_slot` | приём 50 мин | второй приём внахлёст; затем отмена первого | 409 `SLOT_CONFLICT`; после отмены слот свободен |
| `test_terminal_appointment_not_rescheduled_and_not_started` | запись в статусе `no_show` | `PATCH starts_at`, `POST …/start` | 409 `APPOINTMENT_NOT_ACTIVE` в обоих случаях |
| `test_start_appointment_is_idempotent` | запись календаря | два `POST …/start` | 201 и 200 с тем же `session_id`, `has_session=true` |
| `test_offline_session_without_appointment` | пациент roster | `POST /clinician/sessions` без `appointment_id` | 201, `appointment_id=null`, вторая сессия разрешена |
| `test_session_idor_returns_404` | сессия первого врача | запросы второго врача | 404 `SESSION_NOT_FOUND` на GET/finish/PATCH |
| `test_finish_creates_exactly_one_visit` | сессия + подтверждённый протокол | `finish` дважды | ровно один `visits` (по `session_id`), сводка протокола в `notes` |
| `test_session_detail_contains_protocol_and_artifacts` | сессия с протоколом | `GET /sessions/{id}` | `protocol {version,status}`, `artifacts=[]`, `signature/delivery=null` |
| `test_audit_rows_written_for_write_paths` | запись/старт/правка/finish | — | в `audit_log` есть `APPOINTMENT_CREATE/UPDATE`, `SESSION_START/FINISH`; текста нет |
| `test_put_creates_draft_and_stores_ciphertext` | `PUT …/protocol` | сырой `SELECT sections_cipher` | `version=1`, `draft`, в БД bytea без plaintext и имени раздела |
| `test_section_limits_and_icd10` | разделы сверх §5.2 | `PUT …/protocol` | 422 `PROTOCOL_SECTION_TOO_LONG` / `PROTOCOL_ICD10_INVALID` |
| `test_approve_requires_mandatory_sections` | заполнены только жалобы | `approve` | 409 `PROTOCOL_NOT_READY` со списком пустых разделов |
| `test_approve_hashes_canonical_json_and_locks` | полный протокол | `approve`, затем `PUT` | `content_sha256` = sha256 канонического JSON; 409 `PROTOCOL_LOCKED` |
| `test_edit_after_signed_creates_new_version` | версия 1 в статусе `signed` | `PUT…/protocol` | версия 2 `draft`, версия 1 `superseded`, approve новой версии |
| `test_protocol_access_gates` | чужая сессия; отзыв согласия | `GET/PUT/approve` | 404 `SESSION_NOT_FOUND`; после отзыва 403 `CONSENT_REQUIRED` |
| `test_roundtrip_format_and_no_plaintext_on_disk` | байты расшифровки | `put_bytes`/`get_bytes` | `CCART1`+nonce, plaintext на диске отсутствует, sha256 по шифротексту |
| `test_tampered_file_is_detected` | файл артефакта | подмена одного байта | 500 `ARTIFACT_CORRUPT` (не молчаливый мусор) |
| `test_path_outside_root_rejected` / `test_missing_file_reported` | путь из БД / удалённый файл | `get_bytes` | `ARTIFACT_CORRUPT` / `ARTIFACT_MISSING` |
| `test_session_detail_lists_artifacts` | артефакт сессии | `GET /sessions/{id}` | метаданные артефакта видны, в БД нет текста |
| `test_calendar_pagination` | два приёма в окне | `limit=1` / `offset=1` / `limit=201` | страницы по одному, 422 на потолке 200 (§4) |
| `test_consent_update_is_audited` | сессия врача | `PATCH {audio_consent:true}` | 200 и строка `SESSION_CONSENT_UPDATE` по `clinical_session` |
| `test_naive_datetime_rejected` | `starts_at`/`from` без пояса | `POST /appointments`, `GET /appointments` | 422 `VALIDATION_ERROR` вместо `TypeError` |
| `test_diagnosis_code_conflict_rejected` | `sections.diagnosis.icd10=F41.1` | PUT с `diagnosis_code=F41.0` | 422 `PROTOCOL_ICD10_CONFLICT` |
| `test_diagnosis_code_follows_sections_icd10` | PUT без `diagnosis_code` | PUT, затем `approve` | код берётся из разделов и закрепляется в `session_protocols` |
| `test_render_pdf_runs_off_the_event_loop` | подписанный протокол | GET `…/protocol/pdf` при подменённом `documents.render_pdf` | рендер в «AnyIO worker»-потоке, не в event loop (docs/03 §12) |

### 3.2 Async-рантайм, пулы и кеш корпуса

| Тест | Given | When | Then |
|---|---|---|---|
| `test_finish_rolls_back_whole_unit` (`test_async_db.py`) | инъекция сбоя аудита на финише скрининга | POST финиша | нет ни `assessments`, ни `escalations`, ни `notifications` — async-сессия не оставляет частичных строк |
| `test_audit_and_business_write_are_atomic` | сбой записи аудита | доменная операция | бизнес-строка тоже откатана (одна транзакция) |
| `test_no_idle_in_transaction_after_concurrent_requests` | 200 одновременных запросов | после ответов | `pg_stat_activity` не содержит `idle in transaction` — сессия из `get_db` всегда закрывается |
| `test_pool_budget_counts_api_workers_and_background_services` | формула бюджета | `validate_pool_budget(max_workers, background=1)` и `max_workers+1` | впритык проходит, превышение — `RuntimeError`; фоновые процессы входят в тот же бюджет |
| `test_background_profile_has_small_pool_and_sync_engine_is_nullpool` | `db.setup` | проверка профилей | `pool_capacity("background") < pool_capacity()`; `sync_engine.pool` — `NullPool` |
| `test_pool_exhaustion_is_503_with_retry_after` (`test_errors.py`) | маршрут бросает `sqlalchemy.exc.TimeoutError` | GET | 503 `DB_UNAVAILABLE`, заголовок `Retry-After: 2`, `instance` = путь |
| `test_check_rate_does_not_create_entries` (`test_ratelimit.py`) | чистое состояние лимитера | `check_rate` без неудачных попыток | словарь ключей пуст (проверка не создаёт запись) |
| `test_recorded_hits_are_bounded_by_max_keys` | поток неудачных попыток по разным парам (IP, email) | `record_rate_hit` × (`_MAX_KEYS`+50) | размер словаря ≤ `_MAX_KEYS` — память процесса ограничена |
| `test_limit_still_blocks_after_hits` | 5 неудачных входов | `check_rate` | отказ с `retry_after ≥ 1` (лимит не сломан уборкой) |
| `test_second_call_is_cache_hit_without_db_query` (`test_kb_corpus_cache.py`) | корпус собран | повторный `build_corpus()` | сборка из кеша, запросов к `drugs` нет |
| `test_mutation_is_invisible_until_invalidated` | правка справочника | `build_corpus()` без инвалидации, затем `clear_corpus_cache()` | до инвалидации снимок прежний, после — новый |
| `test_ttl_expiry_rebuilds_corpus` / `test_ttl_zero_disables_cache` | TTL истёк / `CC_KB_CORPUS_TTL_SECONDS=0` | вызовы | пересборка / кеш отключён |
| `test_errors_are_not_cached` | сборка бросает исключение | повторный вызов | исключение не закешировано, сборка повторяется |
| `test_request_path_drug_docs_are_not_cached` | `build_corpus(drug_docs=...)` | два вызова с разными препаратами | данные запроса не попадают в общий кеш |
| `test_push_failure_log_has_no_capability_token` (`test_push.py`) | endpoint с токеном в пути | `_endpoint_host` | в лог попадает только хост, токена нет |
| `test_ip_budget_blocks_spray_of_distinct_emails` (`test_ratelimit.py`) | `CC_RATE_IP_MAX` неудачных попыток с одного IP по разным email | проверка входа с этого IP / с другого | отказ с `retry_after ≥ 1`; другой IP не затронут |
| `test_pair_budget_shared_between_processes`, `test_ip_budget_shared_between_processes` (`test_ratelimit_redis.py`) | два «процесса» с общим фейковым Redis | `rate_max`/`rate_ip_max` неудач вперемешку через оба | 429 на обоих: бюджет не умножается на число процессов |
| `test_increments_are_atomic_across_processes` | 8 потоков × 2 клиента, 25 записей каждый | `_redis_record` | ровно 200 в `INCR`-счётчике и TTL на ключе (нет lost update) |
| `test_window_expires_by_ttl`, `test_check_does_not_create_keys` | TTL = 2 окна | `check_rate` после истечения | окно отпускает; проверка не создаёт ключей |
| `test_redis_failure_falls_back_to_memory_and_warns_once`, `test_dead_redis_does_not_delay_login_beyond_timeout` | Redis отвечает ошибкой/спит | `check_rate` | fail-open `(True, 0)`, один WARNING, `redis_errors=1`, задержка ≤ socket_timeout, дальше memory |
| `test_auto_*`, `test_missing_redis_package_degrades`, `test_connect_passes_timeout_and_disables_retries` | нет URL / нет пакета / мёртвый Redis / избыток ретраев | `init_backend`/`_connect` | memory + WARNING; timeout 0.3 с и `Retry(..., 0)` |
| `test_health_reports_rate_limit_backend`, `test_ip_key_is_hmac_with_server_secret`, `test_async_check_does_not_block_event_loop` | правки ревью PR #38: видимость хранилища, необратимость ключа, блокировка event loop | `GET /health`; `_redis_key("ip", …)` при смене `CC_DATA_KEY`; `acheck_rate` при «медленном» Redis (0.2 с) | `rate_limit.backend`/`redis_errors` в ответе; ключ ≠ `SHA-256(ip)`, меняется со сменой секрета; тикер успевает ≥ 5 тиков, т.е. loop не заблокирован |
| `test_reset_clears_redis_store`, `test_reset_clears_memory_store` | активное хранилище с окнами | `reset_rate_limits` | чистится именно активный store, выбор бэкенда сбрасывается |
| `test_migrations_do_not_disable_application_loggers` (`test_async_db.py`) | после `init_db()` (alembic `fileConfig`) | запись в логгер `app.services.push` | логгер не отключён, запись доходит до handler'а |
| `test_thread_message_deadline_is_503_without_assistant_message` (`test_assistant_threads.py`) | агент-заглушка спит дольше `CC_ASSISTANT_DEADLINE_S` | POST сообщения в тред | 503 `ASSISTANT_TIMEOUT`, `Retry-After: 5`, RFC 7807 `instance` = путь; ответ ассистента не персистится (в треде только вопрос) |
| `test_agent_offload_uses_dedicated_limiter` | `AGENT_LIMITER` = `CC_ASSISTANT_THREAD_LIMIT` (8) | offload `_invoke_agent` | `to_thread.run_sync` получает `limiter=AGENT_LIMITER` — общий пул AnyIO не занимается |
| `test_assistant_ask_deadline_is_503_with_retry_after` (`test_assistant.py`) | медленный агент | POST `/assistant/ask` | 503 `ASSISTANT_TIMEOUT` + `Retry-After: 5`, без traceback — тот же маппинг, что у треда |
| `test_assistant_ask_validation_code_unchanged` | пустой `query` | POST `/assistant/ask` | прежний код `422 VALIDATION` (коды ошибок не сдвинулись) |
| `test_report_sql_budget` (`test_clinician.py`) | пациент с ростером и без НЯ, затем с назначением + НЯ | `GET /clinician/patients/{id}/report` под `before_cursor_execute` на `sync_engine` | ровно 11 запросов в обоих случаях (было 12 и 13): анамнез — LEFT JOIN в первом запросе, НЯ — один джойн с назначением и ГРЛС |
| `test_scales_catalog_exposes_anchors`, `test_scales_catalog_roles_and_report_has_no_catalog` | врач, админ и пациент | `GET /clinician/scales`; `GET .../report` | 7 шкал, `anchors == max_value+1`, HDRS-17 h1=5/h4=3; пациенту 403, у админа 200; в `report` ключа `available_scales` больше нет |

### 3.3 ML-сайдкар (stub, `ml/tests`)

`python -m pytest ml/tests -q` — 82 теста без Postgres, Qdrant, torch и GPU
(`CC_ML_STUB=1`, детерминированные заглушки той же формы, что реальные
модели). Контракт — `api/app/services/ml_client.py`, docs/08 §4.1.

| Тест | Сценарий | Ожидание |
|---|---|---|
| `test_health_loading` | до загрузки моделей | 200, `status=loading`, `device=stub` |
| `test_endpoints_503_while_loading` | три эндпоинта до `warmup()` | 503 `model_loading` + `Retry-After` |
| `test_health_ready` | загрузка завершена | `status=ready`, дефолтный профиль и список загруженных |
| `test_invalid_default_profile_rejected` | опечатка в `CC_ML_DEFAULT_PROFILE` | старт падает, а не 500 в запросе |
| `test_models_unavailable_code` | упавшая загрузка | 503 `model_unavailable` |
| `test_token_missing_401` / `_wrong_403` / `_ok` | `CC_ML_TOKEN` задан | 401 без заголовка, 403 с неверным, 200 с верным |
| `test_embeddings_shape_and_dims` | batch, `kind=query` | `profile`, `dense_size=1024`, L2≈1, len=1024 |
| `test_embeddings_*_profile` / `query_and_passage_differ` | смена профиля, префиксы | профиль в ответе; query≠passage |
| `test_embeddings_unknown_profile` | неизвестный профиль | 404 `unknown_profile` |
| `test_embeddings_caps` / `test_rerank_caps` / `test_mask_cap` | >64 текстов, >256 документов, >20000 символов, `top_k` вне диапазона | 422 `invalid_request` |
| `test_rerank_alignment` | 3 документа | `len(scores)==len(documents)`, порядок сохранён |
| `test_mask_spans_and_text` | ФИО+телефон+ДР+СНИЛС | stub: `model=regex`, `degraded=false`, спаны по `start`, категории FIO/PHONE/DOB/DOC, исходные ПДн заменены |
| `test_mask_without_names_keeps_regex_only` | только телефон | regex-путь не трогает время «15:00» |
| `test_startup_requires_token` / `_allows_explicit_anonymous` | пустой `CC_ML_TOKEN` | старт падает; только `CC_ML_ALLOW_ANONYMOUS=1` разрешает анонимно |
| `test_body_too_large_rejected_before_parsing` | тело >1 МиБ на `/v1/mask` | 413 `payload_too_large` до валидации схемы |
| `test_health_error_has_no_details` | упавшая загрузка с путём в ошибке | в `/health` только `error: true`, без путей/токена |
| `test_ner_chunks_cover_long_text` | текст >2 окон | окна ≤1200 символов, перекрытие ≈200, хвост покрыт |
| `test_ner_finds_name_in_last_chunk` | ФИО только в конце 6000+ символов | `model=hybrid`, `[ФИО]`, имя не утекло |
| `test_ner_failure_is_degraded_and_sticky` | pipe бросает исключение | `model=regex`, `degraded=true`, `_broken` залипает |
| `test_health_hides_model_paths` | `CC_ML_*_MODEL=/srv/...` | в `/health` только basename, пути не уходят |

Реальные модели на GPU проверяются отдельным прогоном (не в CI):
`REPORT-ml-sidecar.md` — холодная загрузка, VRAM, p50/p95 всех четырёх
эндпоинтов, фактически замаскированный текст и тексты 393/6000 символов
(ФИО в конце длинного документа находится и маскируется).

## 4. e2e (UI-флоу, playwright против docker compose, `e2e/tests/`)

Навигация пациента: разделы — через меню «Меню» (три полоски), скрининг — кнопкой «Перейти к скринингу» в блоке «Напоминания» главного экрана (или «Пройти скрининг» на баннере), чат — плашкой «Чат с врачом» (хелперы `openPatientMenu`/`patientSection`/`openScreening`/`openPatientChat`).

`pitch.spec.ts` — питч-сценарий (docs/04 §6):
1. Пациент: скрининг по 8 пунктам (PHQ-8, пункт 9 не предъявляется; все по 3 → 24 ⇒ RED) + примечание «Пункт 9 не задавался» + контакты помощи (8-800-2000-122, 112).
2. Свежий пациент с RED-историей: чат-плашка («Чат с врачом») отвечает горячей линией один раз в сутки — второе сообщение сохраняется без автоответа.
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

`daily-today.spec.ts` — единый ежедневный экран «Сегодня» (FR-19, docs/16): первый шаг чек-ина — 7 пунктов ядра, шаги отправляются кнопкой «Далее» (активна при полном шаге), неполный чек-ин не блокирует дневник и приём лекарств, ответ «Да» на HR4 ведёт на шаг безопасности RS1–RS3 (RS3 = «Нет» → экстренный блок), прогресс и состояние дня переживают перезагрузку; дневник — 5-точечное настроение с эмодзи.

`clinician-links.spec.ts` — адресуемость карточки пациента (#patient=<id>): имя в таблице и кнопка «Открыть» дают ссылку, прямая ссылка открывает ту же карточку в новой загрузке, «Назад к списку» снимает hash, клик по уведомлению об эскалации открывает карточку именно этого пациента. FR-47: липкое оглавление карточки (`nav.patient-nav`) с чипами «Риск/Скрининг/Чек-ины/Лекарства/НЯ/История/Динамика»; чип «История» раскрывает свёрнутый блок «Объединённая история и импорт», чип «Динамика» ставит `aria-current`, раскрывает `<details>` и прокручивает блок в окно (липкая строка его не накрывает); на 390px чипы прокручиваются вбок одной строкой и остаются липкими; на 768/1001/1280px центр чипа «Риск» по `elementFromPoint` — сам чип, а не липкая кнопка «Меню».

`assistant.spec.ts` — ИИ-ассистент врача (5 тестов): путь «Чаты → Ассистент», тред отвечает с источниками по KB, отказ на диагноз (red line), справка по препарату Авиандр, пациент в контексте треда подмешивает карту в ответ, поток не дублируется после обновления. FR-47: `Ctrl+Enter` в композере отправляет вопрос (пузырь вопроса, пустой композер, ответ появляется — без ассертов на текст LLM), рядом с композером — подсказка «Ctrl+Enter — отправить».

`chats.spec.ts` — единый экран «Чаты» (FR-29): сегмент «Ассистент» создаёт диалог и получает ответ с цитатами (в CI без LLM допустим NO_KB_DATA); сегмент «Пациенты» показывает roster-переписку с превью последнего сообщения и отправляет ответ врача; старые разделы «Пациенты»/«Библиотека»/«Напоминания» остаются доступны, в списке видна подпись «Сначала красные».

`legal.spec.ts` — правовые страницы (FR-35): `/legal/terms` и `/legal/consent` открываются без входа (нет полей логина, нет запросов к API), содержат плашку «требует юридической проверки» и ключевые разделы согласия (состав данных, pgcrypto, деперсонализация, отзыв согласия), реквизиты оператора — плейсхолдеры; на ширине 390px нет горизонтального скролла; обратная ссылка возвращает на экран входа.

Расширение покрытия (13 файлов, 21 сценарий на 2026-10-26; P0/P1 из плана расширения e2e):

`rbac-idor.spec.ts` — негативные RBAC/IDOR: без токена 401 на клинические/админ-ресурсы; пациент получает 403 ROLE_MISMATCH; врач видит только свой ростер (403 NO_ROSTER на чужого); UI-изоляция вкладок.
`consent-revoke.spec.ts` — отзыв согласия: данные становятся недоступны (403 CONSENT_REQUIRED/404), после повторного согласия дневник пуст (уничтожение ПДн, 152-ФЗ ст.9).
`auth-2fa.spec.ts` — краевые 2FA: врач без OTP / с неверным OTP → 401; пациент входит без 2FA; UI-ошибка и повторный вход с корректным кодом. Негативные попытки идут на одноразового врача, чтобы не исчерпывать rate-limit бюджет `doc@compass.demo`.
`auth-rate-limit.spec.ts` — 5 неудачных входов → 429 TOO_MANY_REQUESTS; другой аккаунт не задет (ключ — IP+email).
`auth-refresh-rotation.spec.ts` — повторное использование отозванного refresh → 401; logout отзывает новый токен.
`multi-scale-screening.spec.ts` — многошкальный режим: врач назначает набор шкал, пациент выбирает GAD-7 из двух и завершает скрининг.
`clinician-adverse-event.spec.ts` — врач регистрирует НЯ (нежелательное явление) в карточке пациента.
`clinician-chat.spec.ts` — чат врач ↔ пациент: врач открывает карточку → «Открыть в чатах», пишет из раздела «Чаты → Пациенты», пациент видит сообщение в своём чате и отвечает; после перезагрузки hash `#chats=patient:<id>` возвращает врача в диалог с ответом.
`clinician-assessment.spec.ts` — врачебная шкала HDRS-17: врач заполняет её в карточке, отчёт сразу показывает RED-эскалацию; каталог пациента клиницистскую шкалу не содержит.
`drug-library.spec.ts` — библиотека препаратов: поиск по ГРЛС, разделы НЯ и противопоказаний, пустой результат.
`clinician-visit.spec.ts` — запись приёма: сохранение, формат/длительность, запись видна после перезагрузки.
`clinician-schedule.spec.ts` — календарь приёмов и ручной протокол: запись/правка/отмена, старт приёма, протокол с подтверждением и persist, приём без записи после перезагрузки.
`clinician-session-documents.spec.ts` — документ приёма (FR-39/40/41): подпись СЭМД и проверка («подпись действительна»), PDF протокола непустой, передача в РЭМД через мок до бейджа «зарегистрирован в РЭМД» (кнопка гаснет); допечатанный во время сохранения/генерации текст врача не выбрасывается.

`patient-home-priority.spec.ts` — главный экран пациента (FR-45/FR-46): напоминания стоят выше баннера скрининга, их кнопки ≥44px и баннер ниже них на 390×844 (замер `boundingBox`, без скриншотов); «Отметить чек-ин»/«Отметить приём» открывают «Сегодня» с прокруткой к блоку и фокусом на первом ответе, «Перейти к скринингу» — скрининг; вопрос приёма читается как «Сколько таблеток вы приняли?», отметки taken/partial(`half`)/missed сохраняются как выбраны (проверка через `/me/adherence`). Копирайт баннера («Врач назначил скрининг: …») и кнопок приёма затронул локаторы `patient-flows.spec.ts`, `consent-revoke.spec.ts`, `rbac-idor.spec.ts`, `daily-today.spec.ts` — они обновлены вместе со спекой.

e2e-хелперы вынесены в `e2e/tests/helpers.ts` (TOTP RFC 4648 base32 + HMAC-SHA1, `login`, `registerPatient`, `freshPatientEmail`, `totp`).

Кэш GET на фронте (`ui/src/api.ts`) — не «вечная» память, а stale-while-revalidate.
Инварианты (не кэшируются ошибки и не-GET, полная инвалидация на мутациях согласия
и logout, `cacheEpoch` против ответов «до действия», дедуп одновременных полётов)
держит `api.ts`; новый контракт — событие `cc:cache-update`: истёкшая запись
(старше `CACHE_TTL_MS` 30 с) отдаётся экрану сразу, фоновый GET её обновляет и шлёт
событие, `useCacheUpdate` (`ui/src/cache.ts`) перечитывает данные конкретных путей
(`ClinicianView` — реестр и открытая карточка, `PatientView` → «Сегодня» — дневник,
приём, чек-ин). Stale ограничен `CACHE_MAX_STALE_MS` (5 мин): более старую запись
экран не получает вовсе — запрос идёт синхронно и его ошибка всплывает, иначе при
затяжном сбое бэкенда карточка показывала бы клинические данные, которым часы, без
индикации сбоя.

## 5. Регрессия и CI

- Подготовка (единый venv): `pip install -e "core[dev]" -e "api[dev]"`; тестовый Postgres — `docker compose -f infra/docker-compose.yml up -d postgres` (тестовая БД `compass_test` создаётся фикстурой, URL в `CC_TEST_DATABASE_URL`).
- Команда прогона: `pytest core api -q` (интеграция — против реального Postgres, не моков).
- Покрытие ядра (DoD E1, ≥95%): `pytest core --cov=compass --cov-fail-under=95 --cov-report=term-missing` (нужен `pytest-cov` в dev-extra).
Предохранитель ML-клиента (docs/12): | ML-клиент | `test_breaker_opens_after_three_network_failures`, `test_breaker_half_open_after_cooldown`, `test_success_resets_failure_counter`, `test_client_4xx_does_not_open_breaker` — предохранитель не ходит в сеть при разомкнутой цепи, пропускает пробу после паузы, не размыкается на 4xx | 4 |

- e2e (UI-флоу): `cd e2e && npx playwright test` — запускать при изменении `ui/` или контрактов API; против поднятого `docker compose up -d postgres api ui`.
- Перед каждым коммитом — полный прогон `pytest core api -q` + (при изменении UI) e2e.

## 6. Ключевые негативные проверки (обязательны к демо)

- Суицидальный пункт не «теряется» при высокой общей сумме и наоборот (таблица 2.2).
- Чек-ин не генерирует вопросов вне шкалы (защита от «мусорной» генерации).
- Аудит нельзя подделать (append-only триггер).
- Доступ врача строго по roster (даже при верном JWT).