"""Ежедневный адаптивный чек-ин: контракт docs/16 §4–§5.

Без моков: реальный Postgres, движок правил core/compass/checkin.py.
"""

from __future__ import annotations

import asyncio

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from app import db
from app.services.checkin import _ensure_escalation
from app.models import AuditLog, CheckinSession, ClinicianRoster, Escalation, Notification

from .conftest import admin_ctx, clinician_ctx, patient_ctx

CORE = {
    "MO1": 0,
    "TR1": 0,
    "SL1": 4,
    "IN1": 0,
    "EN1": 0,
    "FU1": 0,
}

# v2.0.0: гейт безопасности — MO1 >= 3 или FU1 >= 3 или EN1 >= 2.
GATE_CORE = {**CORE, "MO1": 3}
# Без гейта: SL(2) + FU(2) + MO(2) заполняют бюджет 6 модульных пунктов, AP — нет.
RISKY_CORE = {**CORE, "MO1": 2, "SL1": 0, "FU1": 2}


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

def _gate_yes(client, h, session_id: str) -> dict:
    """Ядро с MO1=3 включает гейт: шаг safety из одного HR4, ответ «Да» → RS1–RS3."""
    body = _answer(client, h, session_id, GATE_CORE)
    assert body["phase"] == "safety", body
    assert [i["id"] for i in body["step"]["items"]] == ["HR4"]
    return _answer(client, h, session_id, {"HR4": 1})


def _add_roster(patient_id: str, clinician_id: str) -> None:
    with db.SyncSessionLocal() as s:
        s.add(
            ClinicianRoster(
                patient_id=uuid.UUID(patient_id), clinician_id=uuid.UUID(clinician_id)
            )
        )
        s.commit()


def _escalations(patient_id: str) -> list[Escalation]:
    with db.SyncSessionLocal() as s:
        return list(
            s.scalars(select(Escalation).where(Escalation.patient_id == uuid.UUID(patient_id)))
        )


def _notifications(user_id: str) -> list[Notification]:
    with db.SyncSessionLocal() as s:
        return list(
            s.scalars(select(Notification).where(Notification.user_id == uuid.UUID(user_id)))
        )


def _audit_actions(patient_id: str) -> list[str]:
    with db.SyncSessionLocal() as s:
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
    progress = body["progress"]
    assert progress["answered"] == 0 and progress["planned"] == 6
    assert progress["safety_gate"] is False and progress["safety_gate_reason"]
    assert body["modules"] == [] and body["safety"] == "unassessed"
    assert body["safety_gate"] is False
    assert body["clinician_notified"] is False
    assert body["rules_version"] == "2.0.0"
    assert body["date"] == datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()


def test_start_returns_core_step_of_six_items(client):
    h, _ = patient_ctx(client, "chk_start@test.ru")
    resp = client.post("/api/v1/checkins", headers=h)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "in_progress"
    assert body["phase"] == "core"
    assert body["session_id"]
    # Риск не оценён: HR4 в ядро не входит, гейт решается после ядра.
    assert body["safety"] == "unassessed" and body["safety_gate"] is False
    items = body["step"]["items"]
    assert [i["id"] for i in items] == ["MO1", "TR1", "SL1", "IN1", "EN1", "FU1"]
    assert body["step"]["kind"] == "core"
    for item in items:
        assert set(item) == {"id", "domain", "text", "kind", "anchors", "emoji", "max_value"}
        assert item["kind"] == "scale" and item["max_value"] == 4
    assert items[0]["anchors"] and items[0]["emoji"]
    assert body["progress"]["planned"] == 6


def test_start_is_idempotent(client):
    h, pid = patient_ctx(client, "chk_idem@test.ru")
    first = client.post("/api/v1/checkins", headers=h)
    second = client.post("/api/v1/checkins", headers=h)
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["session_id"] == second.json()["session_id"]
    with db.SyncSessionLocal() as s:
        assert len(s.scalars(select(CheckinSession)).all()) == 1
    assert _audit_actions(pid).count("CHECKIN_START") == 1


def test_today_reflects_open_session(client):
    h, _ = patient_ctx(client, "chk_open@test.ru")
    started = _start(client, h)
    body = client.get("/api/v1/checkins/today", headers=h).json()
    assert body["status"] == "in_progress"
    assert body["session_id"] == started["session_id"]
    assert body["phase"] == "core"
    progress = body["progress"]
    assert progress["answered"] == 0 and progress["planned"] == 6
    assert progress["safety_gate"] is False


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


def test_gate_item_out_of_range_rejected(client):
    h, _ = patient_ctx(client, "chk_gate_range@test.ru")
    sid = _start(client, h)["session_id"]
    _answer(client, h, sid, GATE_CORE)
    resp = client.post(
        f"/api/v1/checkins/{sid}/answers", json={"answers": {"HR4": 2}}, headers=h
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
    # Гейт не сработал: HR4 не задавался, риск остаётся неоценённым — это норма.
    assert body["safety"] == "unassessed"
    assert body["safety_gate"] is False
    assert "HR4" not in body["answers"]
    assert body["completed_at"]
    progress = body["progress"]
    assert progress["answered"] == 6 and progress["planned"] == 6
    assert progress["safety_gate"] is False and progress["safety_gate_reason"]
    assert _escalations(pid) == []
    assert "CHECKIN_COMPLETE" in _audit_actions(pid)
    # после завершения второй сеанс в тот же день невозможен
    again = client.post("/api/v1/checkins", headers=h)
    assert again.status_code == 409
    assert again.json()["code"] == "CHECKIN_ALREADY_DONE"


def test_core_with_symptoms_leads_to_module_step(client):
    h, _ = patient_ctx(client, "chk_modules@test.ru")
    sid = _start(client, h)["session_id"]
    answers = {**CORE, "MO1": 2}
    body = _answer(client, h, sid, answers)
    assert body["status"] == "in_progress"
    assert body["phase"] == "modules"
    assert body["safety_gate"] is False
    ids = set(i["id"] for i in body["step"]["items"])
    assert ids == {"MO2", "MO3", "AP1", "AP2", "AP3"}
    progress = body["progress"]
    assert progress["answered"] == 6 and progress["planned"] == 11
    assert progress["safety_gate"] is False
    codes = {m["code"]: m for m in body["modules"]}
    assert codes["MO"]["included"] is True and codes["MO"]["reason"]
    # HR включается только при MO1 ≥ 3 или FU1 ≥ 3 — сегодня модуль не выбран
    assert "HR" not in codes
    # Повторная отправка ядра на модульном шаге — уже не текущий шаг
    resp = client.post(
        f"/api/v1/checkins/{sid}/answers", json={"answers": CORE}, headers=h
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "CHECKIN_UNKNOWN_ITEM"
    modules = _answer(
        client, h, sid, {"MO2": 0, "MO3": 0, "AP1": 0, "AP2": 0, "AP3": 0}
    )
    assert modules["status"] == "completed"
    assert modules["phase"] == "done"
    assert modules["answers"]["MO1"] == 2
    assert modules["progress"]["answered"] == 11


def test_limit_12_items_and_module_reasons(client):
    h, _ = patient_ctx(client, "chk_limit@test.ru")
    sid = _start(client, h)["session_id"]
    body = _answer(client, h, sid, RISKY_CORE)
    ids = [i["id"] for i in body["step"]["items"]]
    assert body["progress"]["planned"] == 12
    assert body["safety_gate"] is False
    # Бюджет без гейта — 6 модульных пунктов: SL(2) + FU(2) + MO(2).
    assert set(ids) == {"SL2", "SL3", "FU2", "FU3", "MO2", "MO3"}
    codes = {m["code"]: m for m in body["modules"]}
    assert codes["SL"]["included"] is True
    assert codes["FU"]["included"] is True
    assert codes["MO"]["included"] is True
    # AP подходит по правилу (MO1 ≥ 2), но не помещается в лимит — пропуск с причиной
    assert codes["AP"]["included"] is False and "лимит 12" in codes["AP"]["reason"]


# --- Протокол риска ----------------------------------------------------------


def test_gate_fires_after_core_and_asks_only_hr4(client):
    h, _ = patient_ctx(client, "chk_gate@test.ru")
    sid = _start(client, h)["session_id"]
    body = _answer(client, h, sid, GATE_CORE)
    assert body["status"] == "in_progress"
    assert body["phase"] == "safety"
    assert body["step"]["kind"] == "safety"
    assert [i["id"] for i in body["step"]["items"]] == ["HR4"]
    assert body["step"]["items"][0]["text"] == "Были ли у вас сегодня мысли о самоповреждении или о том, чтобы причинить себе вред?"
    assert body["safety"] == "unassessed"
    assert body["safety_gate"] is True
    assert "MO1 ≥ 3" in body["progress"]["safety_gate_reason"]
    # Модули решаются после ответа на HR4; при гейте предъявлено 6 ядра + 1 HR4.
    assert body["progress"]["answered"] == 6 and body["progress"]["planned"] == 7

    body = _answer(client, h, sid, {"HR4": 1})
    assert body["phase"] == "safety"
    assert [i["id"] for i in body["step"]["items"]] == ["RS1", "RS2", "RS3"]
    assert body["progress"]["answered"] == 7 and body["progress"]["planned"] == 10
    assert body["safety"] == "unassessed"  # RS ещё не пройден
    assert body["modules"] == []


def test_gate_yes_no_skips_safety_and_goes_to_modules(client):
    h, pid = patient_ctx(client, "chk_gate_no@test.ru")
    _, cid = clinician_ctx(client, "chk_gate_no_doc@test.ru")
    _add_roster(pid, cid)
    sid = _start(client, h)["session_id"]
    _answer(client, h, sid, GATE_CORE)
    body = _answer(client, h, sid, {"HR4": 0})
    assert body["phase"] == "modules"
    assert body["safety"] == "none"
    assert _escalations(pid) == []
    assert _notifications(cid) == []
    codes = {m["code"]: m for m in body["modules"]}
    assert codes["HR"]["included"] is True
    assert codes["MO"]["included"] is True
    assert body["progress"]["safety_gate"] is True


def test_gate_yes_signal_escalates_to_roster_clinician(client):
    h, pid = patient_ctx(client, "chk_signal@test.ru")
    _, cid = clinician_ctx(client, "chk_doc@test.ru")
    _add_roster(pid, cid)
    sid = _start(client, h)["session_id"]
    _gate_yes(client, h, sid)
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
    body = _gate_yes(client, h, sid)
    assert body["clinician_notified"] is False
    body = _answer(client, h, sid, {"RS1": 0, "RS2": 1, "RS3": 0})
    assert body["safety"] == "signal"
    assert body["clinician_notified"] is False
    (escalation,) = _escalations(pid)
    assert escalation.kind == "CHECKIN_RISK_URGENT"
    assert escalation.notified_clinician_at is None  # уведомлений не было
    with db.SyncSessionLocal() as s:
        assert s.scalars(select(Notification).where(Notification.patient_id == uuid.UUID(pid))).all() == []


def test_gate_yes_without_rs_keeps_escalation_open(client):
    """HR4 = «Да», RS не пройден: сигнал уже в очереди, сеанс не завершён."""
    h, pid = patient_ctx(client, "chk_open@test.ru")
    _, cid = clinician_ctx(client, "chk_open_doc@test.ru")
    _add_roster(pid, cid)
    sid = _start(client, h)["session_id"]
    # Гейт сработал, но HR4 ещё не отвечен — эскалации нет.
    _answer(client, h, sid, GATE_CORE)
    assert _escalations(pid) == []
    body = _answer(client, h, sid, {"HR4": 1})
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
    _gate_yes(client, h, sid)
    body = _answer(client, h, sid, {"RS1": 0, "RS2": 0, "RS3": 1})
    assert body["safety"] == "review"
    (escalation,) = _escalations(pid)
    assert escalation.kind == "CHECKIN_RISK"
    assert escalation.checkin_session_id is not None
    assert len(_notifications(cid)) == 1  # повторное уведомление только для signal


def test_hr4_yes_with_negative_rs_is_self_harm_not_suicidal(client):
    """HR4=«Да» при всех отрицательных RS — самоповреждение, а не «всё хорошо».

    Такое состояние остаётся review (не urgent), но паттерн обязан отличаться от
    «суицидальный риск»: врач не должен читать это как отрицание риска.
    """
    h, pid = patient_ctx(client, "chk_selfharm@test.ru")
    sid = _start(client, h)["session_id"]
    _gate_yes(client, h, sid)
    body = _answer(client, h, sid, {"RS1": 0, "RS2": 0, "RS3": 1})
    assert body["safety"] == "review"
    assert body["risk_assessed"] is True
    assert body["risk_pattern"] == "self_harm"

    ch, _ = clinician_ctx(client, "chk_selfharm_doc@test.ru")
    item = client.get("/api/v1/escalations", headers=ch).json()["items"][0]
    assert item["patient_id"] == pid
    assert item["risk_pattern"] == "self_harm"


def test_unassessed_day_reports_risk_not_assessed(client):
    """Гейт не сработал — риск не оценивался; risk_assessed=False и в API, и в карте."""
    h, pid = patient_ctx(client, "chk_noassess@test.ru")
    state = _start(client, h)
    sid = state["session_id"]
    today = client.get("/api/v1/checkins/today", headers=h).json()
    core = {item["id"]: 0 for item in today["step"]["items"]}
    body = _answer(client, h, sid, core)
    assert body["safety"] == "unassessed"
    assert body["risk_assessed"] is False
    assert body["risk_pattern"] == "unknown"

    ch, cid = clinician_ctx(client, "chk_noassess_doc@test.ru")
    _add_roster(pid, cid)
    report = client.get(f"/api/v1/clinician/patients/{pid}/report", headers=ch)
    assert report.status_code == 200, report.text
    session = report.json()["checkin"]["recent"][0]
    assert session["risk_assessed"] is False
    assert session["risk_pattern"] == "unknown"


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
    _answer(client, h, sid, {**CORE, "MO1": 2})
    session = db_session.get(CheckinSession, uuid.UUID(sid))
    steps = session.presented["steps"]
    assert len(steps) == 1
    assert steps[0]["phase"] == "core"
    assert len(steps[0]["items"]) == 6
    reasons = {m["code"]: m["reason"] for m in session.presented["modules"]}
    assert reasons["MO"].startswith("MO1 ≥ 2")
    assert session.phase == "modules" and session.status == "in_progress"


def test_report_shows_checkin_answers_and_risk(client):
    """docs/16 §3: врач в карте видит ответы чек-ина, риск и основание эскалации."""
    h, pid = patient_ctx(client, "chk_report@test.ru")
    ch, cid = clinician_ctx(client, "chk_report_doc@test.ru")
    _add_roster(pid, cid)
    sid = _start(client, h)["session_id"]
    _gate_yes(client, h, sid)
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
    _gate_yes(client, h, sid)
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
    _gate_yes(client, h, sid)
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
        assert item["in_roster"] is False  # ростера нет; админу флаг всегда false
        assert item["risk_answers"] == {"HR4": 1, "RS1": 0, "RS2": 1, "RS3": 0}
        assert item["risk_pattern"] == "suicidal"
    assert client.get("/api/v1/escalations", headers=h).status_code == 403

    with db.SyncSessionLocal() as s:
        escalation = s.scalar(select(Escalation).where(Escalation.patient_id == uuid.UUID(pid)))
        escalation.resolved_at = datetime.now(timezone.utc)
        s.commit()
    assert client.get("/api/v1/escalations", headers=ch).json()["items"] == []


def test_consent_revoke_removes_checkin_and_escalations(client, db_session):
    h, pid = patient_ctx(client, "chk_revoke@test.ru")
    sid = _start(client, h)["session_id"]
    _gate_yes(client, h, sid)
    _answer(client, h, sid, {"RS1": 1, "RS2": 0, "RS3": 0})
    assert _escalations(pid)

    assert client.post("/api/v1/consent/revoke", headers=h).status_code == 204
    assert _escalations(pid) == []
    with db.SyncSessionLocal() as s:
        assert s.scalar(
            select(CheckinSession).where(CheckinSession.patient_id == uuid.UUID(pid))
        ) is None
        assert s.scalars(
            select(Notification).where(Notification.patient_id == uuid.UUID(pid))
        ).all() == []


def test_resubmit_of_core_step_does_not_duplicate_escalation(client):
    """Повторная отправка завершённого шага ядра не создаёт вторую эскалацию/уведомление."""
    h, pid = patient_ctx(client, "chk_dup_step@test.ru")
    _, cid = clinician_ctx(client, "chk_dup_step_doc@test.ru")
    _add_roster(pid, cid)
    sid = _start(client, h)["session_id"]
    _gate_yes(client, h, sid)
    assert len(_escalations(pid)) == 1
    assert len(_notifications(cid)) == 1

    body = _answer(client, h, sid, CORE, expected=422)
    assert body["code"] == "CHECKIN_UNKNOWN_ITEM"
    assert len(_escalations(pid)) == 1
    assert len(_notifications(cid)) == 1


def test_ensure_escalation_is_single_row_and_upgrades(client):
    """Прямой вызов _ensure_escalation дважды: одна строка, повышение kind, второе уведомление."""
    h, pid = patient_ctx(client, "chk_ensure@test.ru")
    _, cid = clinician_ctx(client, "chk_ensure_doc@test.ru")
    _add_roster(pid, cid)
    sid = _start(client, h)["session_id"]

    async def flow():
        async with db.AsyncSessionLocal() as s:
            session = await s.get(CheckinSession, uuid.UUID(sid))
            first, rec1 = await _ensure_escalation(s, session, urgent=False)
            await s.commit()
            first_kind = first.kind
            again, rec2 = await _ensure_escalation(s, session, urgent=False)
            await s.commit()
            urgent, rec3 = await _ensure_escalation(s, session, urgent=True)
            await s.commit()
            # Строки поднимаются в одной сессии, объект мутирует: снимаем снимок.
            return first.id, first_kind, rec1, again.id, rec2, urgent.id, urgent.kind, rec3

    first_id, first_kind, recipients, again_id, recipients_again, urgent_id, urgent_kind, recipients_urgent = (
        asyncio.run(flow())
    )
    assert first_kind == "CHECKIN_RISK"
    assert recipients == [uuid.UUID(cid)]
    assert again_id == first_id
    assert recipients_again == []  # без повышения повторно не уведомляем
    assert urgent_id == first_id
    assert urgent_kind == "CHECKIN_RISK_URGENT"
    assert recipients_urgent == [uuid.UUID(cid)]

    assert len(_escalations(pid)) == 1
    assert len(_notifications(cid)) == 2


def test_escalation_queue_marks_roster_and_hides_admin(client):
    h, pid = patient_ctx(client, "chk_queue_roster@test.ru")
    ch, cid = clinician_ctx(client, "chk_queue_my_doc@test.ru")
    oh, _ = clinician_ctx(client, "chk_queue_other_doc@test.ru")
    _add_roster(pid, cid)
    sid = _start(client, h)["session_id"]
    _gate_yes(client, h, sid)
    _answer(client, h, sid, {"RS1": 1, "RS2": 0, "RS3": 0})

    mine = client.get("/api/v1/escalations", headers=ch).json()["items"]
    (item,) = mine
    assert item["in_roster"] is True
    other = client.get("/api/v1/escalations", headers=oh).json()["items"]
    (item,) = other
    assert item["in_roster"] is False
    ah, _ = admin_ctx(client, "chk_queue_admin2@test.ru")
    (item,) = client.get("/api/v1/escalations", headers=ah).json()["items"]
    assert item["in_roster"] is False


def test_escalation_queue_window_and_limit(client):
    h1, pid1 = patient_ctx(client, "chk_win1@test.ru")
    h2, pid2 = patient_ctx(client, "chk_win2@test.ru")
    for h in (h1, h2):
        sid = _start(client, h)["session_id"]
        _gate_yes(client, h, sid)
        _answer(client, h, sid, {"RS1": 1, "RS2": 0, "RS3": 0})

    with db.SyncSessionLocal() as s:
        old = s.scalars(select(Escalation).where(Escalation.patient_id == uuid.UUID(pid2))).one()
        old.created_at = datetime.now(timezone.utc) - timedelta(days=91)
        s.commit()

    ch, _ = clinician_ctx(client, "chk_win_doc@test.ru")
    items = client.get("/api/v1/escalations", headers=ch).json()["items"]
    assert [i["patient_id"] for i in items] == [pid1]

    limited = client.get("/api/v1/escalations?limit=1", headers=ch).json()["items"]
    assert len(limited) == 1
    assert client.get("/api/v1/escalations?limit=201", headers=ch).status_code == 422


def test_escalation_resolve_closes_signal(client):
    h, pid = patient_ctx(client, "chk_resolve@test.ru")
    sid = _start(client, h)["session_id"]
    _gate_yes(client, h, sid)
    _answer(client, h, sid, {"RS1": 1, "RS2": 0, "RS3": 0})
    (escalation,) = _escalations(pid)

    url = f"/api/v1/escalations/{escalation.id}/resolve"
    assert client.post(url, json={}, headers=h).status_code == 403  # пациент не закрывает

    ch, cid = clinician_ctx(client, "chk_resolve_doc@test.ru")
    resp = client.post(url, json={"resolution": "Позвонили, состояние стабильно"}, headers=ch)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(escalation.id)
    assert body["resolved_at"] is not None
    assert body["resolution"] == "Позвонили, состояние стабильно"

    assert client.post(url, json={}, headers=ch).status_code == 409
    assert client.get("/api/v1/escalations", headers=ch).json()["items"] == []
    with db.SyncSessionLocal() as s:
        resolved_actions = list(
            s.scalars(select(AuditLog.action).where(AuditLog.actor_id == uuid.UUID(cid)))
        )
    assert "ESCALATION_RESOLVE" in resolved_actions
    assert client.post(
        f"/api/v1/escalations/{uuid.uuid4()}/resolve", json={}, headers=ch
    ).status_code == 404


def test_escalation_resolution_is_encrypted(client):
    h, pid = patient_ctx(client, "chk_res_secret@test.ru")
    sid = _start(client, h)["session_id"]
    _gate_yes(client, h, sid)
    _answer(client, h, sid, {"RS1": 1, "RS2": 0, "RS3": 0})
    (escalation,) = _escalations(pid)
    ch, _ = clinician_ctx(client, "chk_res_secret_doc@test.ru")
    assert (
        client.post(
            f"/api/v1/escalations/{escalation.id}/resolve",
            json={"resolution": "маркер-секрет-42"},
            headers=ch,
        ).status_code
        == 200
    )
    with db.SyncSessionLocal() as s:
        raw, = s.execute(
            text("SELECT resolution::text FROM escalations WHERE id = :id"),
            {"id": escalation.id},
        ).one()
        assert raw.startswith("\\x")
        assert "маркер-секрет-42" not in raw
        assert s.get(Escalation, escalation.id).resolution == "маркер-секрет-42"
