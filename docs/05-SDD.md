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
│   │   ├── checkin.py         # generate_checkin()
│   │   └── trend.py           # trends()
│   └── tests/                 # юнит-тесты ядра
├── api/                       # FastAPI
│   ├── pyproject.toml
│   ├── app/
│   │   ├── main.py            # create_app(), OpenAPI
│   │   ├── db.py, models.py   # SQLAlchemy (DDL из 04 §5)
│   │   ├── auth.py            # JWT, 2FA, RBAC-зависимости
│   │   ├── routers/           # auth, consent, questionnaires, screenings, checkins,
│   │   │                      # diary, chat, clinician, admin
│   │   ├── services/          # screening, checkin, roster, audit, escalation
│   │   ├── gateways/          # SPI: llm.py, tts.py, sms.py (mock-и по умолчанию)
│   │   └── simulator.py       # seed-генератор (FR-14)
│   └── tests/                 # интеграционные тесты (API+БД)
├── ui/                        # React + Vite + TS
│   └── src/  (pages: patient/diary, patient/checkin, patient/chat,
│               clinician/dashboard, clinician/patient, admin/audit, consent)
├── infra/
│   ├── docker-compose.yml     # postgres api ui seed
│   ├── ui/nginx.conf          # nginx: статика + reverse proxy (TLS на пилоте)
│   └── backup.sh  (pg_dump, ротация)
├── docs/                      # 01–06
└── README.md
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
def generate_checkin(scale: Scale, last: ScaleResult | None) -> Checkin
    # 2–5 вопросов: фокус на worst-пунктах последнего прохождения +
    # текущее состояние (общий балл-прокси: 'за последнюю неделю…')
    # invariant: 2 <= len(items) <= 5; повторные чек-ины в тот же день — CONFLICT на API

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

## 7. Границы инкремента

- Не реализуем заранее: push, мобильные клиенты, реальные LLM/TTS-адаптеры, интеграцию МИС (см. 04 §7). Реальные адаптеры — за SPI, mock-и остаются по умолчанию до отдельного решения.