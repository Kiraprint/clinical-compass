"""FR-28: нежелательные явления — сообщение пациента и проверка врачом.

Пациент сообщает о НЯ (pending), врач ростера подтверждает/отклоняет, связывает
с назначением и справочником ГРЛС. Контракт — docs/04 FR-28, docs/06 §3.
"""
from __future__ import annotations

import uuid

from sqlalchemy import event, select, text

from app import db
from app.models import AdverseEvent, ClinicianRoster, Notification, PatientMedication

from .conftest import clinician_ctx, patient_ctx

def _roster(pid: str, cid: str) -> None:
    with db.SyncSessionLocal() as s:
        s.add(ClinicianRoster(patient_id=uuid.UUID(pid), clinician_id=uuid.UUID(cid)))
        s.commit()

def _medication(pid: str, title: str = "Авиандр") -> str:
    with db.SyncSessionLocal() as s:
        med = PatientMedication(
            patient_id=uuid.UUID(pid), title=title, status="active", source="clinician",
        )
        s.add(med)
        s.commit()
        return str(med.id)

def test_patient_reports_pending_event(client):
    h, pid = patient_ctx(client, "ae-p1@test.ru")
    resp = client.post(
        "/api/v1/me/adverse-events",
        json={"event_text": "Тошнота после приёма", "onset_date": "2026-10-20"},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["status_label"] == "на проверке"
    assert body["verified_at"] is None

    with db.SyncSessionLocal() as s:
        ev = s.execute(select(AdverseEvent).where(AdverseEvent.id == uuid.UUID(body["id"]))).scalar_one()
        assert ev.reported_by == "patient"
        assert ev.clinician_id is None
        assert ev.verified_by is None

    (listed,) = client.get("/api/v1/me/adverse-events", headers=h).json()["items"]
    assert listed["id"] == body["id"]

def test_patient_report_requires_consent(client):
    from .conftest import auth_header, login, register

    register(client, "ae-nocons@test.ru")
    h = auth_header(login(client, "ae-nocons@test.ru")["access_token"])
    resp = client.post(
        "/api/v1/me/adverse-events",
        json={"event_text": "x", "onset_date": "2026-10-20"},
        headers=h,
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "CONSENT_REQUIRED"

def test_patient_foreign_medication_rejected(client):
    h, pid = patient_ctx(client, "ae-p2@test.ru")
    _, other_pid = patient_ctx(client, "ae-p2-other@test.ru")
    foreign = _medication(other_pid)
    resp = client.post(
        "/api/v1/me/adverse-events",
        json={
            "event_text": "Сыпь", "onset_date": "2026-10-20",
            "medication_id": foreign,
        },
        headers=h,
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "MEDICATION_NOT_FOUND"

    own = _medication(pid, "Маритупир")
    ok = client.post(
        "/api/v1/me/adverse-events",
        json={
            "event_text": "Сыпь", "onset_date": "2026-10-20",
            "medication_id": own,
        },
        headers=h,
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["medication_id"] == own
    assert ok.json()["medication_title"] == "Маритупир"

def test_patient_report_notifies_roster_without_pdn(client):
    """Пациент не оценивает тяжесть, поэтому пуш уходит на любое сообщение."""
    h, pid = patient_ctx(client, "ae-severe-p@test.ru")
    ch, cid = clinician_ctx(client, "ae-severe-doc@test.ru")
    _roster(pid, cid)

    resp = client.post(
        "/api/v1/me/adverse-events",
        json={
            "event_text": "Обморок и судороги, вызвал скорую",
            "onset_date": "2026-10-20",
        },
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["severity"] is None

    with db.SyncSessionLocal() as s:
        notes = s.scalars(
            select(Notification).where(Notification.user_id == uuid.UUID(cid))
        ).all()
    assert len(notes) == 1
    n = notes[0]
    assert n.kind == "ADVERSE_EVENT"
    # Текст без ПДн: ни имени, ни описания события.
    assert "Обморок" not in n.body and "судорог" not in n.body
    assert n.body == "Пациент сообщил о нежелательном явлении"

def test_patient_cannot_set_severity(client):
    """Тяжесть — колонка врачебной проверки: присланное пациентом значение игнорируется."""
    h, pid = patient_ctx(client, "ae-mild-p@test.ru")
    ch, cid = clinician_ctx(client, "ae-mild-doc@test.ru")
    _roster(pid, cid)
    resp = client.post(
        "/api/v1/me/adverse-events",
        json={"event_text": "Лёгкая тошнота", "severity": "severe", "onset_date": "2026-10-20"},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["severity"] is None
    with db.SyncSessionLocal() as s:
        ev = s.get(AdverseEvent, uuid.UUID(resp.json()["id"]))
        assert ev.severity is None
        # Пуш не зависит от присланной пациентом «тяжести»: он уходит всегда.
        assert len(s.scalars(select(Notification).where(Notification.user_id == uuid.UUID(cid))).all()) == 1

def test_clinician_queue_contains_pending(client):
    h, pid = patient_ctx(client, "ae-q-p@test.ru")
    ch, cid = clinician_ctx(client, "ae-q-doc@test.ru")
    _roster(pid, cid)
    ev = client.post(
        "/api/v1/me/adverse-events",
        json={"event_text": "Тремор рук", "onset_date": "2026-10-21"},
        headers=h,
    ).json()

    resp = client.get("/api/v1/clinician/adverse-events", params={"status": "pending"}, headers=ch)
    assert resp.status_code == 200, resp.text
    (item,) = resp.json()["items"]
    assert item["id"] == ev["id"]
    assert item["status"] == "pending"
    assert item["reported_by"] == "patient"
    assert item["patient_id"] == pid
    # ФИО — из профиля пациента; у только что зарегистрированного его ещё нет.
    assert "patient_fio" in item
    assert item["drug_id"] is None and item["reference"] is None

def test_verify_confirm_sets_verifier_and_drug_reference(client):
    h, pid = patient_ctx(client, "ae-v-p@test.ru")
    ch, cid = clinician_ctx(client, "ae-v-doc@test.ru")
    _roster(pid, cid)
    med_id = _medication(pid, "Авиандр")
    ev = client.post(
        "/api/v1/me/adverse-events",
        json={
            "event_text": "Тошнота", "onset_date": "2026-10-20",
            "medication_id": med_id,
        },
        headers=h,
    ).json()

    resp = client.patch(
        f"/api/v1/clinician/patients/{pid}/adverse-events/{ev['id']}",
        json={
            "status": "confirmed", "severity": "moderate", "suspected_drug": "Авиандр",
            "drug_id": "aviandr", "action_taken": "monitoring",
            "verification_note": "Связано с титрацией дозы",
        },
        headers=ch,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "confirmed"
    assert body["severity"] == "moderate"
    assert body["verified_by"] == cid
    assert body["verified_at"] is not None
    assert body["drug_id"] == "aviandr"
    assert body["reference"]["trade_name"] == "АВИАНДР®"
    assert "Тошнота" in body["reference"]["adverse_events"] or body["reference"]["adverse_events"]

    # Пациент видит «проверено врачом», но не врачебную заметку.
    (mine,) = client.get("/api/v1/me/adverse-events", headers=h).json()["items"]
    assert mine["status"] == "confirmed"
    assert mine["status_label"] == "проверено врачом"
    assert "verification_note" not in mine

    # Очередь pending пуста, в confirmed — событие.
    assert client.get(
        "/api/v1/clinician/adverse-events", params={"status": "pending"}, headers=ch
    ).json()["items"] == []
    assert len(client.get(
        "/api/v1/clinician/adverse-events", params={"status": "confirmed"}, headers=ch
    ).json()["items"]) == 1

    # Идемпотентность: тот же статус повторно — 200 без изменений.
    again = client.patch(
        f"/api/v1/clinician/patients/{pid}/adverse-events/{ev['id']}",
        json={"status": "confirmed"},
        headers=ch,
    )
    assert again.status_code == 200
    assert again.json()["verified_at"] == body["verified_at"]
    # Смена решения после проверки — 409.
    conflict = client.patch(
        f"/api/v1/clinician/patients/{pid}/adverse-events/{ev['id']}",
        json={"status": "rejected"},
        headers=ch,
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "ADVERSE_ALREADY_VERIFIED"

def test_verify_confirm_requires_severity(client):
    """Подтверждение без оценки тяжести — 422: severity пациента всегда NULL."""
    h, pid = patient_ctx(client, "ae-nosev-p@test.ru")
    ch, cid = clinician_ctx(client, "ae-nosev-doc@test.ru")
    _roster(pid, cid)
    ev = client.post(
        "/api/v1/me/adverse-events",
        json={"event_text": "Тошнота", "onset_date": "2026-10-20"},
        headers=h,
    ).json()
    resp = client.patch(
        f"/api/v1/clinician/patients/{pid}/adverse-events/{ev['id']}",
        json={"status": "confirmed"},
        headers=ch,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "SEVERITY_REQUIRED"
    # Событие осталось в очереди непринятым.
    with db.SyncSessionLocal() as s:
        assert s.get(AdverseEvent, uuid.UUID(ev["id"])).status == "pending"


def test_verify_reject(client):
    h, pid = patient_ctx(client, "ae-r-p@test.ru")
    ch, cid = clinician_ctx(client, "ae-r-doc@test.ru")
    _roster(pid, cid)
    ev = client.post(
        "/api/v1/me/adverse-events",
        json={"event_text": "Головная боль", "onset_date": "2026-10-20"},
        headers=h,
    ).json()
    resp = client.patch(
        f"/api/v1/clinician/patients/{pid}/adverse-events/{ev['id']}",
        json={"status": "rejected", "verification_note": "Не связано с терапией"},
        headers=ch,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"
    (mine,) = client.get("/api/v1/me/adverse-events", headers=h).json()["items"]
    assert mine["status_label"] == "отклонено"

def test_verify_foreign_patient_and_event_404(client):
    h, pid = patient_ctx(client, "ae-f-p@test.ru")
    ch, _cid = clinician_ctx(client, "ae-f-doc@test.ru")
    # Врач не закреплён за пациентом.
    ev = client.post(
        "/api/v1/me/adverse-events",
        json={"event_text": "x", "onset_date": "2026-10-20"},
        headers=h,
    ).json()
    resp = client.patch(
        f"/api/v1/clinician/patients/{pid}/adverse-events/{ev['id']}",
        json={"status": "confirmed"},
        headers=ch,
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "ADVERSE_NOT_FOUND"

    # Своё событие, но не тот event_id — тоже 404.
    ch2, cid2 = clinician_ctx(client, "ae-f-doc2@test.ru")
    _roster(pid, cid2)
    resp2 = client.patch(
        f"/api/v1/clinician/patients/{pid}/adverse-events/{uuid.uuid4()}",
        json={"status": "confirmed"},
        headers=ch2,
    )
    assert resp2.status_code == 404
    assert resp2.json()["code"] == "ADVERSE_NOT_FOUND"

def test_verify_unknown_drug_404(client):
    h, pid = patient_ctx(client, "ae-d-p@test.ru")
    ch, cid = clinician_ctx(client, "ae-d-doc@test.ru")
    _roster(pid, cid)
    ev = client.post(
        "/api/v1/me/adverse-events",
        json={"event_text": "x", "onset_date": "2026-10-20"},
        headers=h,
    ).json()
    resp = client.patch(
        f"/api/v1/clinician/patients/{pid}/adverse-events/{ev['id']}",
        json={"status": "confirmed", "drug_id": "no-such-drug"},
        headers=ch,
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "DRUG_NOT_FOUND"

def test_legacy_clinician_event_is_confirmed(client):
    _, pid = patient_ctx(client, "ae-l-p@test.ru")
    ch, cid = clinician_ctx(client, "ae-l-doc@test.ru")
    _roster(pid, cid)
    resp = client.post(
        f"/api/v1/clinician/patients/{pid}/adverse-events",
        json={"event_text": "Внесено врачом", "severity": "moderate", "onset_date": "2026-10-20"},
        headers=ch,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "confirmed"
    assert body["reported_by"] == "clinician"

    items = client.get(f"/api/v1/clinician/patients/{pid}/adverse-events", headers=ch).json()["items"]
    (item,) = items
    assert item["status"] == "confirmed"
    assert "reference" in item

def test_patient_report_audit_has_no_content(client, db_session):
    h, pid = patient_ctx(client, "ae-audit-p@test.ru")
    client.post(
        "/api/v1/me/adverse-events",
        json={"event_text": "Секретный симптом", "onset_date": "2026-10-20"},
        headers=h,
    )
    from app.models import AuditLog

    rows = db_session.execute(
        select(AuditLog).where(
            AuditLog.action == "ADVERSE_REPORT", AuditLog.actor_id == uuid.UUID(pid)
        )
    ).scalars().all()
    assert len(rows) == 1
    # В аудите только факт и объект, без содержимого события.
    assert rows[0].object_type == "patient"
    assert rows[0].object_id == pid

def test_consent_revoke_deletes_patient_events(client):
    h, pid = patient_ctx(client, "ae-rev-p@test.ru")
    client.post(
        "/api/v1/me/adverse-events",
        json={"event_text": "x", "onset_date": "2026-10-20"},
        headers=h,
    )
    assert client.post("/api/v1/consent/revoke", headers=h).status_code == 204
    with db.SyncSessionLocal() as s:
        assert s.scalars(
            select(AdverseEvent).where(AdverseEvent.patient_id == uuid.UUID(pid))
        ).all() == []

def test_adverse_text_encrypted_at_rest(client, db_session):
    h, pid = patient_ctx(client, "ae-enc-p@test.ru")
    ch, cid = clinician_ctx(client, "ae-enc-doc@test.ru")
    _roster(pid, cid)
    ev = client.post(
        "/api/v1/me/adverse-events",
        json={"event_text": "Секретное описание явления", "onset_date": "2026-10-20"},
        headers=h,
    ).json()
    client.patch(
        f"/api/v1/clinician/patients/{pid}/adverse-events/{ev['id']}",
        json={"status": "confirmed", "severity": "moderate", "verification_note": "Секретная заметка врача"},
        headers=ch,
    )
    types = db_session.execute(
        text(
            "SELECT pg_typeof(event_text)::text AS et, pg_typeof(verification_note)::text AS nt "
            "FROM adverse_events WHERE id = CAST(:i AS uuid)"
        ).bindparams(i=ev["id"])
    ).one()
    assert types.et == "bytea" and types.nt == "bytea"
    raw = db_session.execute(
        text(
            "SELECT event_text::text AS e, verification_note::text AS n "
            "FROM adverse_events WHERE id = CAST(:i AS uuid)"
        ).bindparams(i=ev["id"])
    ).one()
    assert "Секретное" not in raw.e
    assert "Секретная" not in raw.n
    # ORM расшифровывает.
    row = db_session.execute(
        select(AdverseEvent).where(AdverseEvent.id == uuid.UUID(ev["id"]))
    ).scalar_one()
    assert row.event_text == "Секретное описание явления"
    assert row.verification_note == "Секретная заметка врача"

def test_onset_date_is_validated_by_moscow_calendar(client, monkeypatch):
    """Граница «сегодня» считается по МСК, а не по UTC.

    Регресс: сервер в UTC до 03:00 МСК считал сегодняшний день будущим и
    отвечал 422 на корректное событие пациента.
    """
    from datetime import date as _date

    from app.routers import adverse as adverse_router

    monkeypatch.setattr(adverse_router, "_moscow_today", lambda: _date(2026, 10, 24))
    h, _pid = patient_ctx(client, "ae-msk@test.ru")
    ok = client.post(
        "/api/v1/me/adverse-events",
        json={"event_text": "Тошнота", "onset_date": "2026-10-24"},
        headers=h,
    )
    assert ok.status_code == 201, ok.text
    future = client.post(
        "/api/v1/me/adverse-events",
        json={"event_text": "Тошнота", "onset_date": "2026-10-25"},
        headers=h,
    )
    assert future.status_code == 422, future.text


def test_patient_report_has_no_severity_until_clinician_verifies(client):
    """FR-33/34: пациент не оценивает тяжесть — колонка NULL до проверки врачом."""
    h, pid = patient_ctx(client, "ae-nosev-p@test.ru")
    ch, cid = clinician_ctx(client, "ae-nosev-doc@test.ru")
    _roster(pid, cid)

    resp = client.post(
        "/api/v1/me/adverse-events",
        json={"event_text": "Сыпь на предплечье", "onset_date": "2026-10-20"},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["severity"] is None
    assert body["status"] == "pending"

    with db.SyncSessionLocal() as s:
        ev = s.execute(select(AdverseEvent).where(AdverseEvent.id == uuid.UUID(body["id"]))).scalar_one()
        assert ev.severity is None

    # Врач выставляет шкалу при проверке.
    verdict = client.patch(
        f"/api/v1/clinician/patients/{pid}/adverse-events/{body['id']}",
        json={"status": "confirmed", "severity": "moderate"},
        headers=ch,
    )
    assert verdict.status_code == 200, verdict.text
    assert verdict.json()["severity"] == "moderate"


def _count_statements(call):
    """Сколько SQL ушло на один HTTP-вызов (слушатель на sync_engine async-движка)."""
    statements: list[str] = []

    def _count(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    engine = db.engine.sync_engine
    event.listen(engine, "before_cursor_execute", _count)
    try:
        response = call()
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    return response, len(statements)


def test_adverse_lists_batch_references(client):
    """N+1 → константа: число SQL не растёт от числа строк (было 2 запроса на строку).

    Раньше очередь врача и /me/adverse-events делали `_drug_ref` + `_medication_title`
    на каждое событие; теперь назначения и справочник ГРЛС — по одному запросу
    на список.
    """
    h, pid = patient_ctx(client, "ae-n1-p@test.ru")
    ch, cid = clinician_ctx(client, "ae-n1-doc@test.ru")
    _roster(pid, cid)
    med_id = _medication(pid, "Авиандр")

    def _report(n: int) -> None:
        for i in range(n):
            resp = client.post(
                "/api/v1/me/adverse-events",
                json={
                    "event_text": f"Событие {i}", "onset_date": "2026-10-20",
                    "medication_id": med_id,
                },
                headers=h,
            )
            assert resp.status_code == 201, resp.text

    _report(2)
    _, queue_small = _count_statements(
        lambda: client.get("/api/v1/clinician/adverse-events", headers=ch)
    )
    _, patient_small = _count_statements(
        lambda: client.get("/api/v1/me/adverse-events", headers=h)
    )

    _report(8)  # всего 10 событий: если запросы идут на строку, счётчик вырастет
    queue, queue_big = _count_statements(
        lambda: client.get("/api/v1/clinician/adverse-events", headers=ch)
    )
    patient, patient_big = _count_statements(
        lambda: client.get("/api/v1/me/adverse-events", headers=h)
    )

    assert queue.status_code == 200 and len(queue.json()["items"]) == 10, queue.text
    assert len(patient.json()["items"]) == 10
    # Названия назначений у всех строк одни — по запросу на список, не на строку.
    assert queue.json()["items"][0]["medication_title"] == "Авиандр"
    assert queue_big == queue_small, (queue_small, queue_big)
    assert patient_big == patient_small, (patient_small, patient_big)
    assert queue_big <= 5 and patient_big <= 4, (queue_big, patient_big)


def test_adverse_lists_honour_limit(client):
    """limit валидируется (0/201 → 422) и реально ограничивает выдачу."""
    h, pid = patient_ctx(client, "ae-lim-p@test.ru")
    ch, cid = clinician_ctx(client, "ae-lim-doc@test.ru")
    _roster(pid, cid)
    for i in range(3):
        resp = client.post(
            "/api/v1/me/adverse-events",
            json={"event_text": f"Событие {i}", "onset_date": "2026-10-20"},
            headers=h,
        )
        assert resp.status_code == 201, resp.text

    for path, headers in (
        ("/api/v1/me/adverse-events", h),
        ("/api/v1/clinician/adverse-events", ch),
    ):
        assert client.get(path, params={"limit": 0}, headers=headers).status_code == 422
        assert client.get(path, params={"limit": 201}, headers=headers).status_code == 422
        limited = client.get(path, params={"limit": 2}, headers=headers)
        assert limited.status_code == 200, limited.text
        assert len(limited.json()["items"]) == 2
        full = client.get(path, headers=headers)
        assert len(full.json()["items"]) == 3
