# 05. SDD — Spec-Driven Design (дизайн → контракты → реализация)

Статус: v0.1, 2026-10-09. Порядок работы: контракт фиксируется (этот документ) → пишется красный тест (06) → реализация → верификация. Никакой реализации без контракта.

---

## 1. Принципы SDD на проекте

1. **Контракт — источник правды**: типы интерфейсов `core`, сигнатуры, HTTP-contract из 04. Изменение контракта = осознанное решение + обновление теста.
2. **Трейсабельность**: каждый FR (04 §1) → эпик (ниже) → тест (06). В DoD эпика — ссылка на закрытые FR.
3. **Реализация только под красный тест**: сначала тест (fail), затем минимальная реализация (pass), затем рефакторинг при зелёном.
4. **Ядро — без I/O**: в `core` запрещены import БД/HTTP/сеть; интерфейсы через `typing.Protocol`.
5. Зелёный тест не означает DoD: проверить интеграцию (API+БД) и демо-прогон.

## 2. Карта репозитория

```
clinical-compass/
├── core/                      # SDK «клинический движок» (pip install clinical-compass-core)
│   ├── pyproject.toml
│   ├── compass/
│   │   ├── __init__.py
│   │   ├── models.py          # Scale, ScaleResult, Triage, RiskFlag, Checkin, Trend
│   │   ├── scales.py          # PHQ-9, GAD-7, HADS-A, HADS-D (данные + пороги)
│   │   ├── scoring.py         # score(), risk_flags()
│   │   ├── triage.py          # triage()
│   │   ├── checkin.py         # банк пунктов + plan()/safety_level()
│   │   └── trend.py           # trends()
│   └── tests/                 # юнит-тесты ядра
├── api/                       # FastAPI
│   ├── pyproject.toml
│   ├── app/
│   │   ├── main.py            # create_app(), OpenAPI
│   │   ├── db.py, models.py   # SQLAlchemy (DDL из 04 §5)
│   │   ├── auth.py            # JWT, 2FA, RBAC-зависимости
│   │   ├── routers/           # auth, consent, questionnaires, screenings, checkins,
│   │   │                      # diary, chat, clinician, admin, kb_admin
│   │   ├── services/          # screening, checkin, roster, audit, escalation,
│   │   │                      # agent, kb, embeddings, llm_router, push, depersonalize
│   │   ├── enrichment/        # PubMed/ВОЗ → критерии → реестр kb_works → Qdrant (docs/15)
│   │   ├── gateways/          # SPI: llm.py, tts.py, sms.py (mock-и по умолчанию)
│   │   └── simulator.py       # seed-генератор (FR-14)
│   └── tests/                 # интеграционные тесты (API+БД)
├── ui/                        # React + Vite + TS
│   └── src/  (pages: patient/diary, patient/checkin, patient/chat,
│               clinician/dashboard, clinician/patient, admin/audit, admin/kb, consent)
├── infra/
│   ├── docker-compose.yml     # postgres qdrant api kb-seed kb-enrichment ui seed
│   ├── docker-compose.dev.yml # только postgres+qdrant для локальных тестов
│   ├── ui/nginx.conf          # nginx: статика + reverse proxy (TLS на пилоте)
│   └── backup.sh  (pg_dump, ротация)
├── docs/                      # 01–15 + ТЗ, SDD/TDD, SECURITY-AUDIT
├── .worktrees/                # локальные git worktree (не в git), см. §2.1
└── README.md
```

### 2.1. Изоляция работы над фичами

Каждая фича разрабатывается в отдельном worktree **внутри** репозитория:
`git worktree add .worktrees/<name> -b <branch>`, каталог `.worktrees/` в
`.git/info/exclude`. Соседние каталоги `../clinical-compass-*` не используются.
```

## 3. Контракты ядра (сигнатуры — то, что реализуем и тестируем)

```python
# docs контракта: типы из core/compass/models.py
class ScaleResult:
    code: str          # 'PHQ-9'
    total_score: int
    severity: Severity # NONE | MILD | MODERATE | SEVERE
    per_item: dict[str, int]   # item_key -> value (для risk-флагов и дельты чек-ина)

# scoring.py
def score(scale: Scale, answers: Mapping[str, int]) -> ScaleResult
    # raises:
    #   UnknownItemError (item_key нет в шкале)              — 422 на API
    #   IncompleteAnswersError (нет всех item_key шкалы)      — 422 на API
    #   ValueError (value вне 0..max_value пункта)            — 422 на API
    # invariant: total_score == sum(per_item.values()) по items шкалы
def risk_flags(scale: Scale, answers: Mapping[str, int]) -> list[RiskFlag]
    # Обобщённые сигналы из структуры шкалы (без привязки к расстройству — диагноз за врачом):
    #   любой scale.suicidal_item (пункт о суициде, PHQ-9 item 9) value >= 1 → RiskFlag('SUICIDAL_IDEATION')
    #   сумма баллов достигла собственного порога SEVERE шкалы → RiskFlag('SEVERE_SYMPTOMS')
    #   [пороги severity и суицидального пункта — подтвердить медиком команды]

# triage.py
def triage(result: ScaleResult, flags: Sequence[RiskFlag]) -> Triage  # GREEN|YELLOW|RED
    # RED: любой SUICIDAL_IDEATION ИЛИ severity==SEVERE
    # YELLOW: severity в (MILD, MODERATE)
    # GREEN: severity == NONE и флагов нет
    # таблица порогов severity — в scales.py, медик валидирует [TBD]
    # порог «item 9 PHQ-9 >= 1 → SUICIDAL_IDEATION» — тоже клиническое число [TBD]

# checkin.py
def plan(answers: Mapping[str, int]) -> CheckinPlan
    # фаза core|safety|modules|done: ядро 7 пунктов, затем модули по 9 правилам
    # приоритета целиком, риск-гейт HR4 → модуль безопасности RS1–RS3
    # invariant: len(planned_items(answers)) <= 12; одинаковые ответы → одинаковый шаг
    # повторный чек-ин в тот же день — CONFLICT на API (checkin_sessions)

def safety_level(answers: Mapping[str, int]) -> str
    # none|review|signal|unassessed; risk не суммируется с симптомами

# trend.py
def trends(runs: Sequence[ScaleResult]) -> Trend
    # moving_avg (окно 7), delta_7, delta_30, slope (линейная регрессия)
    # invariant: пустой вход → пустой тренд (не кидать)
```

## 4. Эпики (порядок реализации)

| Эпик | Содержание | Закрывает FR | DoD |
|---|---|---|---|
| E1 | Ядро: модели, шкалы, scoring, triage, risk_flags | FR-2, FR-3, FR-4 | юнит-тесты E1 зелёные, coverage ≥ 95% |
| E2 | Ядро: checkin, trend | FR-6, FR-7 | юнит-тесты E2 зелёные |
| E3 | API-скелет: auth, RBAC, consent, аудит | FR-1, FR-11, FR-12, FR-13 | интеграционные тесты (AuthFlow, ConsentFlow, AuditAppendOnly) |
| E4 | API: screenings, checkins, escalation | FR-3, FR-4, FR-5, FR-6 | тест EscalationWithin1s (транзакция finish) |
| E5 | API: diary, chat (mock gateways), clinician panel, report | FR-8, FR-9, FR-10 | интеграционные тесты roster-доступа и report |
| E6 | UI: patient app, clinician dashboard, admin; simulator seed | FR-14 + всё | e2e-сценарий питча (04 §6) проходит на docker compose |
| E7 | ИИ-агент: KB (Qdrant) + гибридный поиск + LangGraph + UI-вкладка | FR-17 | тесты агента (red line, цитаты, offtopic) + e2e вкладки «Ассистент» |
| E8 | `kb-enrichment`: источники PubMed/ВОЗ, критерии, ревью, производный индекс | FR-20 | оффлайн-тесты коннекторов/критериев/пайплайна + прогон, воспроизводимый по `kb_runs` |
| E9 | Сессии клинического ассистента: треды, LLM-переформулировка уточняющих вопросов (retrieval/раздел), мультипациентская область, UI-колонка диалогов | FR-21 | `test_assistant_threads.py` (изоляция врачей, roster-гейт, переформулировка/fallback/ПДн, red line по текущему вопросу, аудит, каскад) + `test_llm_router.py` + `npm run build` |
| E10 | Интеграция с ЕМИАС: host-side клиент (OAuth/FHIR, ретраи, allowlist), идемпотентный sync в `emias_links/patient_medications/visits`, админ-API + CLI, мок-сервис `emias-mock`, блок «ЕМИАС» в карточке врача | FR-25 | оффлайн-тесты мока/клиента/маппинга/sync/API на реальном Postgres; повторный прогон без дублей; `docker compose config` валиден |
| E11 | Контакты пациента в карточке: `users.phone` (шифруется) + `users.birth_date`, нормализация РФ-номера, маскирование в витринах, self-service пациента, уничтожение при отзыве согласия | FR-22 | `test_patient_contacts.py` (RBAC/roster, нормализация и границы даты, маска в списке, аудит, отзыв согласия) + `npm run build` |
| E12 | История приёма и назначений: `patient_medications` для назначений врача (`source`, `clinician_id`, `ended_on`, `notes`; импорт ЕМИАС read-only), CRUD назначений и остановка приёма, единая лента карточки (приёмы/назначения/скрининги/НЯ) с курсором и фильтром типов, блоки «Назначения» и «История пациента» в UI | FR-23 | `test_patient_medications.py`, `test_patient_history.py` (roster/IDOR, шифрование, EMIAS read-only, валидация, курсор, аудит, отзыв согласия) + `npm run build` |
| E13 | Напоминания врача по пациентам: `clinician_reminders` (kind/note/sha), roster-скоуп, фильтры срока, done/cancel, доставка воркером `python -m app.reminders_worker` (advisory-lock, `notifications.reminder_id`), панель «Напоминания» и бейдж просроченных в UI | FR-24 | `test_reminders.py` (RBAC/IDOR, фильтры срока, переходы, шифрование note, тик воркера + идемпотентность, advisory-lock, отзыв согласия) + `docker compose config` + `npm run build` |
| E14 | НЯ от пациента и проверка врачом: `adverse_events` (`reported_by`, `status`, `verified_by/at`, `verification_note`, `drug_id` → справочник ГРЛС, `medication_id` → назначение), очередь врача и PATCH проверки, уведомление ростерных врачей о серьёзном НЯ без ПДн, UI пациента (форма + статусы + 112 при severe) и карточки врача (плашка «НЯ на проверке», форма проверки, фрагмент справочника) | FR-28 | `test_adverse_events.py` (pending/confirm/reject, идемпотентность и 409, 404/422, уведомление без ПДн, шифрование, отзыв согласия) + `npm run build` |

## 5. DoD (Definition of Done) эпика

- [ ] Контракт зафиксирован (сигнатуры, схемы из 03/04) без TODO-заглушек.
- [ ] Красный тест написан до реализации (git-история показывает TDD-порядок).
- [ ] Тест зелёный; интеграция (API+БД) проверена HTTP-запросом.
- [ ] FR закрыты, трейсабельность в комментарии PR/коммита.
- [ ] Демо-прогон на docker compose проходит (для E3–E6).
- [ ] Безопасность: нет ПДн в логах, нет доступа вне roster (проверено тестом).

## 6. Трейсабельность FR → эпик → тест

| FR | Эпик | Ключевой тест (06) |
|---|---|---|
| FR-1 | E3 | `login_clinician_requires_otp`, `register_clinician_returns_totp_secret` |
| FR-2 | E1 | `questionnaire_catalog_serves_scales` |
| FR-3 | E4 | `screening_flow_phq9`, `patient_cannot_choose_scale`, `prescribe_scale_applies_to_patient` |
| FR-4 | E1 | `triage_red_suicidal`, `triage_yellow`, `triage_green` |
| FR-5 | E4 | `escalation_response_under_1s`, `suicidal_answer_escalates` |
| FR-6 | E4 | `checkin_once_per_day` |
| FR-7 | E2 | `trend_delta_30`, `trend_moving_avg_same_length`, `trend_slope_linear` |
| FR-8 | E5 | `clinician_patients_list_and_triage_filter`, `clinician_cannot_see_unrostered` |
| FR-9 | E5 | `diary_in_report` |
| FR-10 | E5 | `chat_hotline_on_red`, `chat_faq_for_green` |
| FR-11 | E3 | `screening_requires_consent`, `consent_create_get_revoke_cycle` |
| FR-12 | E3 | `audit_log_append_only` |
| FR-13 | E3 | `patient_cannot_access_clinician_routes`, `foreign_run_hidden` |
| FR-14 | E6 | покрывается e2e-питч-сценарием (`seed` → RED-траектории), не юнит-тестом |
| FR-15 | E5/E6 | `red_escalation_notifies_rostered_clinician`, `push_subscribe_unsubscribe` (API) + e2e «колокол уведомлений» |
| FR-16 | E7 | `fcm_device_token_register_delete`, `fcm_upgrade_then_cooldown` (API) + CI `release-android.yml` (Capacitor-сборка APK) |
| FR-17 | E7 | `test_assistant.py`, `test_llm_router.py` |
| FR-18 | E4 | `test_clinician.py` (CLINICIAN-шкалы) |
| FR-19 | E5 | `test_patient_flows.py` (чек-ин + дневник + приём) |
| FR-20 | E8 | `test_enrichment_domain.py`, `test_enrichment_pubmed.py`, `test_enrichment_who.py`, `test_enrichment_pipeline.py`, `test_kb_admin_api.py` |
| FR-21 | E9 | `test_assistant_threads.py` (изоляция, roster-гейт, переформулировка уточняющих вопросов и fallback, red line по текущему вопросу, аудит, каскад) |
| FR-22 | E11 | `test_patient_contacts.py` (RBAC, нормализация телефона, границы даты рождения, маска, аудит, отзыв согласия) |
| FR-23 | E12 | `test_patient_medications.py`, `test_patient_history.py` (roster/IDOR, шифрование в покое, EMIAS read-only, валидация дат, курсор/фильтр ленты, аудит, отзыв согласия) |
| FR-24 | E13 | `test_reminders.py` (RBAC/IDOR, фильтры overdue/today/week, done/cancel, шифрование note, тик воркера и идемпотентность, advisory-lock, отзыв согласия) |
| FR-25 | E10 | `test_emias_mock.py`, `test_emias_client.py`, `test_emias_mapping.py`, `test_emias_sync.py`, `test_emias_api.py` |
| FR-28 | E14 | `test_adverse_events.py` (pending/confirm/reject, 409 `ADVERSE_ALREADY_VERIFIED`, 404 `ADVERSE_NOT_FOUND`/`DRUG_NOT_FOUND`, 422 `MEDICATION_NOT_FOUND`, уведомление без ПДн, шифрование `event_text`/`verification_note`, отзыв согласия) |

## 7. Границы инкремента

- Не реализуем заранее: реальные LLM/TTS-адаптеры, интеграцию МИС (см. 04 §7). Push реализованы в FR-15/FR-16 (Web Push VAPID + уведомления в приложении + нативный FCM-канал Android APK, `mobile/`); реальные адаптеры — за SPI, mock-и остаются по умолчанию до отдельного решения.