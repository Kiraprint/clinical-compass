"""Ежедневный адаптивный чек-ин: контракт docs/16 §4–§5.

Без моков: реальный Postgres, движок правил core/compass/checkin.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from app import db
from app.models import AuditLog, CheckinSession, ClinicianRoster, Escalation, Notification

from .conftest import admin_ctx, clinician_ctx, patient_ctx

CORE = {
    "MO1": 0,
    "TR1": 0,
    "SL1": 4,
    "IN1": 0,
    "EN1": 0,
    "FU1": 0,
    "HR4": 0,
}

# МО1 ≥ 3 → HR(3 пункта); SL1 ≤ 2 → SL(2); остальное не помещается в лимит.
RISKY_CORE = {**CORE, "MO1": 4, "SL1": 0}


def _start(client, h) -> dict:
    resp = client.post("/api/v1/checkins", headers=h)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _answer(client, h, session_id: str, answers: dict, expected: int = 200) -> dict:
    resp = client.post(
        f"/api/v1/checkins/{session_id}/answers", json={"answers": answers}, headers=h
    )
    assert resp.status_code == expected, resp.text
    return resp.json()


def _add_roster(patient_id: str, clinician_id: str) -> None:
    with db.SessionLocal() as s:
        s.add(
            ClinicianRoster(
                patient_id=uuid.UUID(patient_id), clinician_id=uuid.UUID(clinician_id)
            )
        )
        s.commit()


def _escalations(patient_id: str) -> list[Escalation]:
    with db.SessionLocal() as s:
        return list(
            s.scalars(select(Escalation).where(Escalation.patient_id == uuid.UUID(patient_id)))
        )


def _notifications(user_id: str) -> list[Notification]:
    with db.SessionLocal() as s:
        return list(
            s.scalars(select(Notification).where(Notification.user_id == uuid.UUID(user_id)))
        )


def _audit_actions(patient_id: str) -> list[str]:
    with db.SessionLocal() as s:
        return list(
            s.scalars(
                select(AuditLog.action).where(AuditLog.actor_id == uuid.UUID(patient_id))
            )
        )


# --- Состояние до старта и старт ---------------------------------------------


def test_today_not_started(client):
    h, _ = patient_ctx(client, "chk_today@test.ru")
    resp = client.get("/api/v1/checkins/today", headers=h)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "not_started"
    assert body["session_id"] is None and body["phase"] is None
    assert body["step"] == {"kind": None, "items": []}
    assert body["answers"] == {}
    assert body["progress"] == {"answered": 0, "planned": 7}
    assert body["modules"] == [] and body["safety"] == "none"
    assert body["clinician_notified"] is False
    assert body["rules_version"] == "1.0.0"
    assert body["date"] == datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()


def test_start_returns_core_step_of_seven_items(client):
    h, _ = patient_ctx(client, "chk_start@test.ru")
    resp = client.post("/api/v1/checkins", headers=h)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "in_progress"
    assert body["phase"] == "core"
    assert body["session_id"]
    assert body["safety"] == "none"
    items = body["step"]["items"]
    assert [i["id"] for i in items] == ["MO1", "TR1", "SL1", "IN1", "EN1", "FU1", "HR4"]
    assert body["step"]["kind"] == "core"
    for item in items:
        assert set(item) == {"id", "domain", "text", "kind", "anchors", "emoji", "max_value"}
    assert items[6]["kind"] == "gate"
    assert items[0]["anchors"] and items[0]["emoji"]
    assert items[0]["max_value"] == 4 and items[6]["max_value"] == 1


def test_start_is_idempotent(client):
    h, pid = patient_ctx(client, "chk_idem@test.ru")
    first = client.post("/api/v1/checkins", headers=h)
    second = client.post("/api/v1/checkins", headers=h)
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["session_id"] == second.json()["session_id"]
    with db.SessionLocal() as s:
        assert len(s.scalars(select(CheckinSession)).all()) == 1
    assert _audit_actions(pid).count("CHECKIN_START") == 1


def test_today_reflects_open_session(client):
    h, _ = patient_ctx(client, "chk_open@test.ru")
    started = _start(client, h)
    body = client.get("/api/v1/checkins/today", headers=h).json()
    assert body["status"] == "in_progress"
    assert body["session_id"] == started["session_id"]
    assert body["phase"] == "core"
    assert body["progress"] == {"answered": 0, "planned": 7}


# --- Валидация шага ----------------------------------------------------------


def test_partial_step_rejected_incomplete(client):
    h, _ = patient_ctx(client, "chk_partial@test.ru")
    sid = _start(client, h)["session_id"]
    resp = client.post(
        f"/api/v1/checkins/{sid}/answers",
        json={"answers": {"MO1": 0, "TR1": 0}},
        headers=h,
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "CHECKIN_INCOMPLETE"


def test_unknown_item_rejected(client):
    h, _ = patient_ctx(client, "chk_unknown@test.ru")
    sid = _start(client, h)["session_id"]
    resp = client.post(
        f"/api/v1/checkins/{sid}/answers",
        json={"answers": {**CORE, "XX9": 1}},
        headers=h,
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "CHECKIN_UNKNOWN_ITEM"


def test_item_of_later_step_rejected(client):
    """Модульный пункт на шаге ядра — не предъявлен, значит UNKNOWN_ITEM."""
    h, _ = patient_ctx(client, "chk_notcurrent@test.ru")
    sid = _start(client, h)["session_id"]
    resp = client.post(
        f"/api/v1/checkins/{sid}/answers",
        json={"answers": {**CORE, "SL2": 1}},
        headers=h,
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "CHECKIN_UNKNOWN_ITEM"


def test_out_of_range_and_bool_rejected(client):
    h, _ = patient_ctx(client, "chk_range@test.ru")
    sid = _start(client, h)["session_id"]
    for bad in (5, -1, True, "2", 1.5, None):
        resp = client.post(
            f"/api/v1/checkins/{sid}/answers",
            json={"answers": {**CORE, "MO1": bad}},
            headers=h,
        )
        assert resp.status_code == 422, bad
        assert resp.json()["code"] == "CHECKIN_OUT_OF_RANGE", bad
    # HR4 — бинарный: 2 вне диапазона
    resp = client.post(
        f"/api/v1/checkins/{sid}/answers", json={"answers": {**CORE, "HR4": 2}}, headers=h
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "CHECKIN_OUT_OF_RANGE"


# --- Маршрутизация -----------------------------------------------------------


def test_core_without_symptoms_completes_without_modules(client):
    h, pid = patient_ctx(client, "chk_clean@test.ru")
    sid = _start(client, h)["session_id"]
    body = _answer(client, h, sid, CORE)
    assert body["status"] == "completed"
    assert body["phase"] == "done"
    assert body["step"] == {"kind": "done", "items": []}
    assert body["safety"] == "none"
    assert body["completed_at"]
    assert body["progress"] == {"answered": 7, "planned": 7}
    assert _escalations(pid) == []
    assert "CHECKIN_COMPLETE" in _audit_actions(pid)
    # после завершения второй сеанс в тот же день невозможен
    again = client.post("/api/v1/checkins", headers=h)
    assert again.status_code == 409
    assert again.json()["code"] == "CHECKIN_ALREADY_DONE"


def test_core_with_symptoms_leads_to_module_step(client):
    h, _ = patient_ctx(client, "chk_modules@test.ru")
    sid = _start(client, h)["session_id"]
    answers = {**CORE, "MO1": 3}
    body = _answer(client, h, sid, answers)
    assert body["status"] == "in_progress"
    assert body["phase"] == "modules"
    assert set(i["id"] for i in body["step"]["items"]) == {"HR1", "HR2", "HR3", "MO2", "MO3"}
    assert body["progress"] == {"answered": 7, "planned": 12}
    codes = {m["code"]: m for m in body["modules"]}
    assert codes["HR"]["included"] is True and codes["HR"]["reason"]
    assert codes["MO"]["included"] is True
    # AP не помещается в остаток лимита: модуль пропускается целиком, но причина видна
    assert codes["AP"]["included"] is False and "лимит 12" in codes["AP"]["reason"]
    # Повторная отправка ядра на модульном шаге — уже не текущий шаг
    resp = client.post(
        f"/api/v1/checkins/{sid}/answers", json={"answers": CORE}, headers=h
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "CHECKIN_UNKNOWN_ITEM"
    modules = _answer(client, h, sid, {"HR1": 0, "HR2": 0, "HR3": 0, "MO2": 0, "MO3": 0})
    assert modules["status"] == "completed"
    assert modules["phase"] == "done"
    assert modules["answers"]["MO1"] == 3
    assert modules["progress"]["answered"] == 12


def test_limit_12_items_and_module_reasons(client):
    h, _ = patient_ctx(client, "chk_limit@test.ru")
    sid = _start(client, h)["session_id"]
    body = _answer(client, h, sid, RISKY_CORE)
    ids = [i["id"] for i in body["step"]["items"]]
    assert body["progress"]["planned"] <= 12
    # HR (приоритет 1) и SL (приоритет 2) заполнили бюджет целиком
    assert set(ids) == {"HR1", "HR2", "HR3", "SL2", "SL3"}
    codes = {m["code"]: m for m in body["modules"]}
    assert codes["HR"]["included"] is True
    assert codes["SL"]["included"] is True
    skipped = [m for m in body["modules"] if not m["included"]]
    assert skipped and all("лимит 12" in m["reason"] for m in skipped)


# --- Протокол риска ----------------------------------------------------------


def test_gate_yes_routes_to_safety_module_only(client):
    h, _ = patient_ctx(client, "chk_gate@test.ru")
    sid = _start(client, h)["session_id"]
    body = _answer(client, h, sid, {**CORE, "HR4": 1})
    assert body["status"] == "in_progress"
    assert body["phase"] == "safety"
    assert body["step"]["kind"] == "safety"
    assert [i["id"] for i in body["step"]["items"]] == ["RS1", "RS2", "RS3"]
    assert body["progress"] == {"answered": 7, "planned": 10}
    assert body["modules"] == []  # симптоматические модули в этот день не предъявляются


def test_gate_yes_signal_escalates_to_roster_clinician(client):
    h, pid = patient_ctx(client, "chk_signal@test.ru")
    _, cid = clinician_ctx(client, "chk_doc@test.ru")
    _add_roster(pid, cid)
    sid = _start(client, h)["session_id"]
    _answer(client, h, sid, {**CORE, "HR4": 1})
    body = _answer(client, h, sid, {"RS1": 1, "RS2": 0, "RS3": 0})
    assert body["status"] == "completed"
    assert body["safety"] == "signal"
    assert body["clinician_notified"] is True

    (escalation,) = _escalations(pid)  # одна строка на сеанс, обновлена до срочной
    assert escalation.kind == "CHECKIN_RISK_URGENT"
    assert str(escalation.checkin_session_id) == sid
    assert escalation.assessment_id is None
    assert escalation.notified_clinician_at is not None
    notes = _notifications(cid)
    assert len(notes) == 2  # первый сигнал (HR4) + повторный срочный
    assert all(n.escalation_id == escalation.id for n in notes)
    assert any("СРОЧНО" in n.title for n in notes)
    actions = _audit_actions(pid)
    assert "CHECKIN_COMPLETE" in actions and "CHECKIN_RISK_SIGNAL" in actions


def test_gate_yes_signal_without_roster_escalates_silently(client):
    h, pid = patient_ctx(client, "chk_noroster@test.ru")
    sid = _start(client, h)["session_id"]
    body = _answer(client, h, sid, {**CORE, "HR4": 1})
    assert body["clinician_notified"] is False
    body = _answer(client, h, sid, {"RS1": 0, "RS2": 1, "RS3": 0})
    assert body["safety"] == "signal"
    assert body["clinician_notified"] is False
    (escalation,) = _escalations(pid)
    assert escalation.kind == "CHECKIN_RISK_URGENT"
    assert escalation.notified_clinician_at is None  # уведомлений не было
    with db.SessionLocal() as s:
        assert s.scalars(select(Notification).where(Notification.patient_id == uuid.UUID(pid))).all() == []


def test_gate_yes_without_rs_keeps_escalation_open(client):
    """HR4 = «Да», RS не пройден: сигнал уже в очереди, сеанс не завершён."""
    h, pid = patient_ctx(client, "chk_open@test.ru")
    _, cid = clinician_ctx(client, "chk_open_doc@test.ru")
    _add_roster(pid, cid)
    sid = _start(client, h)["session_id"]
    body = _answer(client, h, sid, {**CORE, "HR4": 1})
    assert body["status"] == "in_progress"
    assert body["phase"] == "safety"
    assert body["clinician_notified"] is True
    (escalation,) = _escalations(pid)
    assert escalation.kind == "CHECKIN_RISK"
    assert escalation.notified_clinician_at is not None
    assert len(_notifications(cid)) == 1


def test_gate_yes_review_state_keeps_primary_escalation(client):
    h, pid = patient_ctx(client, "chk_review@test.ru")
    _, cid = clinician_ctx(client, "chk_review_doc@test.ru")
    _add_roster(pid, cid)
    sid = _start(client, h)["session_id"]
    _answer(client, h, sid, {**CORE, "HR4": 1})
    body = _answer(client, h, sid, {"RS1": 0, "RS2": 0, "RS3": 1})
    assert body["safety"] == "review"
    (escalation,) = _escalations(pid)
    assert escalation.kind == "CHECKIN_RISK"
    assert escalation.checkin_session_id is not None
    assert len(_notifications(cid)) == 1  # повторное уведомление только для signal


def test_completed_session_rejects_answers(client):
    h, _ = patient_ctx(client, "chk_done@test.ru")
    sid = _start(client, h)["session_id"]
    _answer(client, h, sid, CORE)
    resp = client.post(f"/api/v1/checkins/{sid}/answers", json={"answers": {"RS1": 0}}, headers=h)
    assert resp.status_code == 409
    assert resp.json()["code"] == "CHECKIN_ALREADY_DONE"


def test_foreign_and_missing_session_404(client):
    h, _ = patient_ctx(client, "chk_owner@test.ru")
    other_h, _ = patient_ctx(client, "chk_other@test.ru")
    sid = _start(client, h)["session_id"]
    resp = client.post(
        f"/api/v1/checkins/{sid}/answers", json={"answers": CORE}, headers=other_h
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "CHECKIN_NOT_FOUND"
    resp = client.post(
        f"/api/v1/checkins/{uuid.uuid4()}/answers", json={"answers": CORE}, headers=h
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "CHECKIN_NOT_FOUND"


# --- Хранение -----------------------------------------------------------------


def test_answers_encrypted_at_rest(client, db_session):
    h, _ = patient_ctx(client, "chk_crypto@test.ru")
    sid = _start(client, h)["session_id"]
    _answer(client, h, sid, CORE)
    raw = db_session.execute(
        text("SELECT answers::text, presented::text FROM checkin_sessions WHERE id = :id"),
        {"id": sid},
    ).one()
    answers_raw, presented_raw = raw
    assert answers_raw.startswith("\\x")  # bytea, а не JSON
    assert "MO1" not in answers_raw and "MO1" not in presented_raw
    # pgcrypto-расшифровка через ORM возвращает исходные значения
    session = db_session.get(CheckinSession, uuid.UUID(sid))
    assert session.answers == CORE
    assert session.presented["steps"][0]["phase"] == "core"
    assert session.presented["modules"] == []


def test_presented_keeps_steps_and_module_reasons(client, db_session):
    h, _ = patient_ctx(client, "chk_presented@test.ru")
    sid = _start(client, h)["session_id"]
    _answer(client, h, sid, {**CORE, "MO1": 3})
    session = db_session.get(CheckinSession, uuid.UUID(sid))
    steps = session.presented["steps"]
    assert len(steps) == 1
    assert steps[0]["phase"] == "core"
    assert len(steps[0]["items"]) == 7
    reasons = {m["code"]: m["reason"] for m in session.presented["modules"]}
    assert reasons["HR"].startswith("MO1 ≥ 3")
    assert session.phase == "modules" and session.status == "in_progress"


def test_report_shows_checkin_answers_and_risk(client):
    """docs/16 §3: врач в карте видит ответы чек-ина, риск и основание эскалации."""
    h, pid = patient_ctx(client, "chk_report@test.ru")
    ch, cid = clinician_ctx(client, "chk_report_doc@test.ru")
    _add_roster(pid, cid)
    sid = _start(client, h)["session_id"]
    _answer(client, h, sid, {**RISKY_CORE, "HR4": 1})
    _answer(client, h, sid, {"RS1": 1, "RS2": 0, "RS3": 0})

    report = client.get(f"/api/v1/clinician/patients/{pid}/report", headers=ch)
    assert report.status_code == 200, report.text
    body = report.json()
    (today,) = body["checkin"]["recent"]
    assert today["session_id"] == sid
    assert today["safety"] == "signal"
    assert today["answers"]["HR4"] == 1 and today["answers"]["RS1"] == 1
    assert today["rules_version"] == body["checkin"]["rules_version"]
    (escalation,) = [e for e in body["open_escalations"] if e["kind"] == "CHECKIN_RISK_URGENT"]
    assert escalation["checkin_session_id"] == sid


# --- Эскалации: уникальность и общая очередь ---------------------------------


def test_repeat_submit_does_not_duplicate_escalation(client):
    h, pid = patient_ctx(client, "chk_dup@test.ru")
    _, cid = clinician_ctx(client, "chk_dup_doc@test.ru")
    _add_roster(pid, cid)
    sid = _start(client, h)["session_id"]
    _answer(client, h, sid, {**CORE, "HR4": 1})
    _answer(client, h, sid, {"RS1": 1, "RS2": 0, "RS3": 0})
    assert len(_escalations(pid)) == 1

    resp = client.post(
        f"/api/v1/checkins/{sid}/answers",
        json={"answers": {"RS1": 1, "RS2": 0, "RS3": 0}},
        headers=h,
    )
    assert resp.status_code == 409
    assert len(_escalations(pid)) == 1
    assert len(_notifications(cid)) == 2  # без третьего «срочного» дубля


def test_escalation_queue_visible_to_staff_only(client):
    h, pid = patient_ctx(client, "chk_queue@test.ru")
    sid = _start(client, h)["session_id"]
    _answer(client, h, sid, {**CORE, "HR4": 1})
    _answer(client, h, sid, {"RS1": 0, "RS2": 1, "RS3": 0})  # signal без roster

    ch, _ = clinician_ctx(client, "chk_queue_doc@test.ru")
    ah, _ = admin_ctx(client, "chk_queue_admin@test.ru")
    for staff in (ch, ah):
        resp = client.get("/api/v1/escalations", headers=staff)
        assert resp.status_code == 200, resp.text
        (item,) = resp.json()["items"]
        assert item["patient_id"] == pid
        assert item["kind"] == "CHECKIN_RISK_URGENT"
        assert item["safety"] == "signal"
        assert item["checkin_session_id"] == sid
        assert item["created_at"] is not None
    assert client.get("/api/v1/escalations", headers=h).status_code == 403

    with db.SessionLocal() as s:
        escalation = s.scalar(select(Escalation).where(Escalation.patient_id == uuid.UUID(pid)))
        escalation.resolved_at = datetime.now(timezone.utc)
        s.commit()
    assert client.get("/api/v1/escalations", headers=ch).json()["items"] == []


def test_consent_revoke_removes_checkin_and_escalations(client, db_session):
    h, pid = patient_ctx(client, "chk_revoke@test.ru")
    sid = _start(client, h)["session_id"]
    _answer(client, h, sid, {**CORE, "HR4": 1})
    _answer(client, h, sid, {"RS1": 1, "RS2": 0, "RS3": 0})
    assert _escalations(pid)

    assert client.post("/api/v1/consent/revoke", headers=h).status_code == 204
    assert _escalations(pid) == []
    with db.SessionLocal() as s:
        assert s.scalar(
            select(CheckinSession).where(CheckinSession.patient_id == uuid.UUID(pid))
        ) is None
        assert s.scalars(
            select(Notification).where(Notification.patient_id == uuid.UUID(pid))
        ).all() == []
