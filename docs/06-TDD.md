# 06. TDD — тесты по контрактам

Статус: v0.1, 2026-10-09. Рабочий цикл: **red** (тест падает с ожидаемой причиной) → **green** (минимальная реализация) → **refactor** (при зелёном). Регрессия — полный прогон перед каждым коммитом.

---

## 1. Стратегия

- Пирамида (фактически достигнуто, см. `core/tests` + `api/tests`): unit (ядро) 88, integration (API+БД) 222 (из них 78 — обогащение КБ, FR-20), e2e (UI-флоу) 33. Числа в 05/06 задавались как ориентир; приоритет — честная трассировка, а не набивка количества.
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
| test_pss10_reverse_scoring_concrete_total | положительные пункты 4/5/7/8 | score(PSS10) | обратный подсчёт, конкретная сумма |
| test_pss10_severity_boundaries | пороги 13/14 и 26/27 | severity_for() | NONE/MILD/MODERATE без SEVERE |
| test_hdrs17_severity_boundaries / test_hdrs17_triage | HAM-D17 (8/17/24), h3 как суицидальный пункт | severity_for()/triage() | MODERATE и RED при h3≥1 |
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
| auth_otp_optional_flag | CC_REQUIRE_OTP=false → login CLINICIAN без otp → 200; секрет остаётся в БД; prod_mode+false → fail-fast |
| auth_config_public | публичный GET /auth/config → {require_otp} по CC_REQUIRE_OTP (UI скрывает поле 2FA) |
| llm_router_* | LLM-роутер раздела: tool_call search_<раздел> → topic, off→None, ошибка/таймаут/неизвестная тулза/без тулзы → None (fallback все разделы) |
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
| `test_thread_create_message_and_read_back` | тред с 2 пациентами → вопрос → чтение | оба сообщения и цитаты в истории, карты обоих пациентов в ответе, дедуп `patient_ids` |
| `test_foreign_thread_is_404` | чужой тред: GET/PATCH/POST/DELETE | 404 `THREAD_NOT_FOUND`, чужой тред не виден в списке |
| `test_patient_ids_roster_gate` | пациент вне roster в `patient_ids` | 403 `PATIENT_NOT_IN_ROSTER` (создание и сообщение) |
| `test_patient_limit` | >10 `patient_ids` | 422 `TOO_MANY_PATIENTS` |
| `test_read_thread_with_patients_is_audited` | чтение треда с пациентами | запись `READ_ASSISTANT_THREAD` в audit_log |
| `test_delete_thread_cascades` | DELETE треда | 404 после удаления, сообщения и привязки удалены каскадом |
| `test_redline_in_history_does_not_poison_next_turn` | ход 1 — redline (диагноз), ход 2 — обычный вопрос по КБ | ход 2 не refused и возвращает KB-цитаты (red line только по текущему вопросу) |
| `test_revoked_consent_skips_patient_card` | отзыв согласия после создания треда | `skipped_patient_ids == [p1]`, карты p1 нет в цитатах, запись `READ_ASSISTANT_THREAD` |
| `test_retrieval_uses_only_current_question` | ФИО в прошлой реплике | агент получает только текущий вопрос, `retrieval_query` не передаётся |
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
| `test_worker_tick_creates_one_notification_idempotent` | два тика `deliver_due` по одному due-напоминанию | ровно один `Notification kind='REMINDER'`; `notified_at` заполнен; второй тик — 0 |
| `test_worker_respects_advisory_lock` | лок удерживает другой коннект | тик возвращает 0 и не создаёт уведомление; после снятия лока — доставка |
| `test_consent_revocation_destroys_reminders` | напоминание задано → POST /consent/revoke | строк пациента в `clinician_reminders` нет |

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

`daily-today.spec.ts` — единый ежедневный экран «Сегодня» (FR-19): чек-ин + дневник + приём лекарств сохраняются одной кнопкой (сообщение «Сохранено: чек-ин · дневник · приём лекарств»), состояние дня переживает перезагрузку, история приверженности видит сегодняшнюю отметку, а неполный чек-ин не блокирует дневник и приём (подсказка «ответьте ещё на N»).

`clinician-links.spec.ts` — адресуемость карточки пациента (#patient=<id>): имя в таблице и кнопка «Открыть» дают ссылку, прямая ссылка открывает ту же карточку в новой загрузке, «Назад к списку» снимает hash, клик по уведомлению об эскалации открывает карточку именно этого пациента.

`assistant.spec.ts` — ИИ-ассистент врача (3 теста): ответ с источниками по KB, отказ на диагноз (red line), справка по препарату Авиандр, @-упоминание пациента подгружает контекст.

Расширение покрытия (8 файлов, 12 тестов; P0/P1 из плана расширения e2e):

`rbac-idor.spec.ts` — негативные RBAC/IDOR: без токена 401 на клинические/админ-ресурсы; пациент получает 403 ROLE_MISMATCH; врач видит только свой ростер (403 NO_ROSTER на чужого); UI-изоляция вкладок.
`consent-revoke.spec.ts` — отзыв согласия: данные становятся недоступны (403 CONSENT_REQUIRED/404), после повторного согласия дневник пуст (уничтожение ПДн, 152-ФЗ ст.9).
`auth-2fa.spec.ts` — краевые 2FA: врач без OTP / с неверным OTP → 401; пациент входит без 2FA; UI-ошибка и повторный вход с корректным кодом. Негативные попытки идут на одноразового врача, чтобы не исчерпывать rate-limit бюджет `doc@compass.demo`.
`auth-rate-limit.spec.ts` — 5 неудачных входов → 429 TOO_MANY_REQUESTS; другой аккаунт не задет (ключ — IP+email).
`auth-refresh-rotation.spec.ts` — повторное использование отозванного refresh → 401; logout отзывает новый токен.
`multi-scale-screening.spec.ts` — многошкальный режим: врач назначает набор шкал, пациент выбирает GAD-7 из двух и завершает скрининг.
`clinician-adverse-event.spec.ts` — врач регистрирует НЯ (нежелательное явление) в карточке пациента.
`clinician-chat.spec.ts` — чат врач ↔ пациент: обе стороны видят переписку.
`clinician-assessment.spec.ts` — врачебная шкала HDRS-17: врач заполняет её в карточке, отчёт сразу показывает RED-эскалацию; каталог пациента клиницистскую шкалу не содержит.
`drug-library.spec.ts` — библиотека препаратов: поиск по ГРЛС, разделы НЯ и противопоказаний, пустой результат.
`clinician-visit.spec.ts` — запись приёма: сохранение, формат/длительность, запись видна после перезагрузки.

e2e-хелперы вынесены в `e2e/tests/helpers.ts` (TOTP RFC 4648 base32 + HMAC-SHA1, `login`, `registerPatient`, `freshPatientEmail`, `totp`).

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