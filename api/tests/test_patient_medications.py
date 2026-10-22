"""FR-23: назначения препаратов в карточке — CRUD, EMIAS read-only, шифрование, аудит."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select, text

from app.models import AuditLog, PatientMedication

from .conftest import admin_ctx, clinician_ctx, patient_ctx

PHQ_ALL = {f"q{i}": 0 for i in range(1, 10)}

def _setup(client):
    """Пациент с согласием + врач в roster.

    Возвращает (clinician_headers, clinician_id, patient_id, patient_headers).
    """
    ph, pid = patient_ctx(client, "med-p@test.ru")
    c, cid = clinician_ctx(client, "med-c@test.ru")
    a, _ = admin_ctx(client, "med-a@test.ru")
    resp = client.post("/api/v1/admin/roster", json={"patient_id": pid, "clinician_id": cid}, headers=a)
    assert resp.status_code == 201, resp.text
    return c, cid, pid, ph

def _create(client, c, pid, **overrides):
    body = {
        "title": "Сертралин",
        "dosage": "50 мг вечером",
        "started_on": "2026-10-01",
        "notes": "Титрация с 25 мг",
        **overrides,
    }
    return client.post(f"/api/v1/clinician/patients/{pid}/medications", json=body, headers=c)

def _actions(client, a) -> list[str]:
    items = client.get("/api/v1/admin/audit", headers=a).json()["items"]
    return [i["action"] for i in items]

def test_create_list_and_audit(client):
    c, cid, pid, _ph = _setup(client)
    a, _ = admin_ctx(client, "med-a2@test.ru")

    resp = _create(client, c, pid)
    assert resp.status_code == 201, resp.text
    med = resp.json()
    assert med["title"] == "Сертралин"
    assert med["status"] == "active"
    assert med["source"] == "clinician"
    assert med["editable"] is True
    assert med["ended_on"] is None

    items = client.get(f"/api/v1/clinician/patients/{pid}/medications", headers=c).json()["items"]
    assert len(items) == 1 and items[0]["id"] == med["id"]
    assert "PRESCRIBE_MEDICATION" in _actions(client, a)

def test_validation_rejects_bad_status_and_dates(client):
    c, cid, pid, _ph = _setup(client)
    assert _create(client, c, pid, status="unknown").status_code == 422
    assert _create(client, c, pid, title="   ").status_code == 422
    assert _create(client, c, pid, started_on="2999-01-01").status_code == 422
    assert _create(client, c, pid, started_on="2026-10-10", ended_on="2026-10-01").status_code == 422

def test_update_fields_and_audit(client):
    c, cid, pid, _ph = _setup(client)
    a, _ = admin_ctx(client, "med-a3@test.ru")
    med_id = _create(client, c, pid).json()["id"]

    resp = client.patch(
        f"/api/v1/clinician/patients/{pid}/medications/{med_id}",
        json={"dosage": "100 мг", "notes": "Утром", "ended_on": "2026-12-01"},
        headers=c,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dosage"] == "100 мг" and body["notes"] == "Утром"
    assert body["ended_on"] == "2026-12-01"
    assert "UPDATE_MEDICATION" in _actions(client, a)

    bad = client.patch(
        f"/api/v1/clinician/patients/{pid}/medications/{med_id}",
        json={"title": "другое"},
        headers=c,
    )
    assert bad.status_code == 422

def test_stop_sets_today_and_status(client):
    c, cid, pid, _ph = _setup(client)
    a, _ = admin_ctx(client, "med-a4@test.ru")
    med_id = _create(client, c, pid, started_on="2026-01-01").json()["id"]

    resp = client.post(f"/api/v1/clinician/patients/{pid}/medications/{med_id}/stop", headers=c)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "stopped"
    assert body["ended_on"] is not None
    assert "STOP_MEDICATION" in _actions(client, a)

    # фильтр и active_only отражают переход статуса
    assert client.get(f"/api/v1/clinician/patients/{pid}/medications?status=stopped", headers=c).json()["items"]
    assert client.get(f"/api/v1/clinician/patients/{pid}/medications?active_only=true", headers=c).json()["items"] == []

def test_delete_local_row(client, db_session):
    c, cid, pid, _ph = _setup(client)
    a, _ = admin_ctx(client, "med-a5@test.ru")
    med_id = _create(client, c, pid).json()["id"]

    resp = client.delete(f"/api/v1/clinician/patients/{pid}/medications/{med_id}", headers=c)
    assert resp.status_code == 204, resp.text
    assert db_session.get(PatientMedication, uuid.UUID(med_id)) is None
    assert "DELETE_MEDICATION" in _actions(client, a)

def test_emias_rows_read_only(client, db_session):
    c, cid, pid, _ph = _setup(client)
    med = PatientMedication(
        patient_id=uuid.UUID(pid), title="Гидроксизин", dosage="25 мг",
        started_on=date(2026, 1, 1), status="active", source="emias", emias_id="med-readonly",
    )
    db_session.add(med)
    db_session.commit()
    med_id = str(med.id)

    patch = client.patch(
        f"/api/v1/clinician/patients/{pid}/medications/{med_id}", json={"dosage": "50 мг"}, headers=c
    )
    assert patch.status_code == 409
    assert patch.json()["code"] == "MEDICATION_IMPORTED"

    stop = client.post(f"/api/v1/clinician/patients/{pid}/medications/{med_id}/stop", headers=c)
    assert stop.status_code == 409
    assert stop.json()["code"] == "MEDICATION_IMPORTED"

    delete = client.delete(f"/api/v1/clinician/patients/{pid}/medications/{med_id}", headers=c)
    assert delete.status_code == 409
    assert delete.json()["code"] == "MEDICATION_IMPORTED"

    # строка не изменилась и видна в списке с source='emias'
    items = client.get(f"/api/v1/clinician/patients/{pid}/medications", headers=c).json()["items"]
    assert items[0]["source"] == "emias" and items[0]["editable"] is False
    assert items[0]["dosage"] == "25 мг"

def test_roster_idor_and_other_clinician(client):
    c, cid, pid, _ph = _setup(client)
    # не в roster → 403 NO_ROSTER (как у остальных клиницистских маршрутов)
    stranger, _ = clinician_ctx(client, "med-stranger@test.ru")
    assert client.get(f"/api/v1/clinician/patients/{pid}/medications", headers=stranger).status_code == 403
    assert _create(client, stranger, pid).json()["code"] == "NO_ROSTER"

    med_id = _create(client, c, pid).json()["id"]
    # назначение другого пациента неотличимо от несуществующего
    _h, other_pid = patient_ctx(client, "med-other@test.ru")
    a, _ = admin_ctx(client, "med-a6@test.ru")
    client.post("/api/v1/admin/roster", json={"patient_id": other_pid, "clinician_id": cid}, headers=a)
    resp = client.patch(
        f"/api/v1/clinician/patients/{other_pid}/medications/{med_id}", json={"dosage": "1"}, headers=c
    )
    assert resp.status_code == 404

    # второй врач в roster того же пациента не правит чужое назначение
    c2, c2id = clinician_ctx(client, "med-c2@test.ru")
    client.post("/api/v1/admin/roster", json={"patient_id": pid, "clinician_id": c2id}, headers=a)
    resp = client.patch(
        f"/api/v1/clinician/patients/{pid}/medications/{med_id}", json={"dosage": "1"}, headers=c2
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "NOT_OWNER"

def test_medication_encrypted_at_rest(client, db_session):
    c, cid, pid, _ph = _setup(client)
    med_id = _create(client, c, pid, title="Сертралин", notes="Суицидальные мысли отрицает").json()["id"]

    raw = db_session.execute(
        text("select title, notes from patient_medications where id = :id"), {"id": uuid.UUID(med_id)}
    ).one()
    for value in raw:
        blob = bytes(value)
        assert isinstance(blob, bytes)
        assert "Сертралин".encode() not in blob
        assert "Суицидальные".encode() not in blob
    # ORM расшифровывает обратно
    med = db_session.get(PatientMedication, uuid.UUID(med_id))
    db_session.refresh(med)
    assert med.title == "Сертралин" and med.notes.startswith("Суицидальные")

def test_consent_revocation_clears_medications(client, db_session):
    c, cid, pid, ph = _setup(client)
    _create(client, c, pid)
    assert db_session.scalars(
        select(PatientMedication).where(PatientMedication.patient_id == uuid.UUID(pid))
    ).all()

    # пациент отзывает согласие своим токеном → назначения уничтожаются
    assert client.post("/api/v1/consent/revoke", headers=ph).status_code == 204
    remaining = db_session.scalars(
        select(PatientMedication).where(PatientMedication.patient_id == uuid.UUID(pid))
    ).all()
    assert remaining == []

    # без согласия врач не видит назначения (данные уничтожены, но гейт тоже проверяем)
    resp = client.get(f"/api/v1/clinician/patients/{pid}/medications", headers=c)
    assert resp.status_code == 403
    assert resp.json()["code"] == "CONSENT_REQUIRED"
