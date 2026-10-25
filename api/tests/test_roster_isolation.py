"""FR-37: изоляция roster — второй врач не видит пациентов первого.

Сценарий демо: doc@ и doc2@ с непересекающимися ростерами. Проверяем список
пациентов, карточку, визиты, назначения, анамнез, напоминания, эскалации,
попытку назначить скрининг и доступ пациента к чужому врачу.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from .conftest import admin_ctx, clinician_ctx, patient_ctx


def _doctor(client, email: str) -> tuple[dict, str]:
    return clinician_ctx(client, email)


def _patient_with_consent(client, email: str) -> tuple[dict, str]:
    return patient_ctx(client, email)


def _roster(client, admin: dict, clinician_id: str, patient_id: str) -> None:
    resp = client.post(
        "/api/v1/admin/roster",
        json={"patient_id": patient_id, "clinician_id": clinician_id},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text


def _setup(client):
    admin, _ = admin_ctx(client, "iso-admin@test.ru")
    h1, p1 = _patient_with_consent(client, "iso-p1@test.ru")
    h2, p2 = _patient_with_consent(client, "iso-p2@test.ru")
    d1, c1 = _doctor(client, "iso-doc1@test.ru")
    d2, c2 = _doctor(client, "iso-doc2@test.ru")
    # p1 принадлежит первому врачу, p2 — второму; росторы не пересекаются.
    _roster(client, admin, c1, p1)
    _roster(client, admin, c2, p2)
    return h1, p1, d1, c1, h2, p2, d2, c2


def test_doc2_sees_only_own_patients(client):
    _, p1, d1, _, _, p2, d2, _ = _setup(client)

    own1 = client.get("/api/v1/clinician/patients", headers=d1).json()["items"]
    own2 = client.get("/api/v1/clinician/patients", headers=d2).json()["items"]
    assert [i["patient_id"] for i in own1] == [p1]
    assert [i["patient_id"] for i in own2] == [p2]


def test_doc2_foreign_card_sections_are_403(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "anamnesis_enabled", True)
    _, p1, _, _, _, _, d2, _ = _setup(client)

    checks = (
        client.get(f"/api/v1/clinician/patients/{p1}/report", headers=d2),
        client.get(f"/api/v1/clinician/patients/{p1}/visits", headers=d2),
        client.get(f"/api/v1/clinician/patients/{p1}/medications", headers=d2),
        client.get(f"/api/v1/clinician/patients/{p1}/anamnesis", headers=d2),
        client.put(
            f"/api/v1/clinician/patients/{p1}/anamnesis", json={"brief": "x"}, headers=d2
        ),
        client.post(
            f"/api/v1/clinician/patients/{p1}/anamnesis/draft", json={"brief": "x"}, headers=d2
        ),
    )
    for resp in checks:
        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "NO_ROSTER", resp.text


def test_doc2_reminders_and_escalations_are_empty_or_out_of_roster(client, db_session):
    _, p1, d1, _, _, p2, d2, _ = _setup(client)
    due = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert client.post(
        "/api/v1/clinician/reminders",
        json={"patient_id": p1, "kind": "call", "note": "позвонить", "due_at": due},
        headers=d1,
    ).status_code == 201

    # Напоминания фильтруются по clinician_id: у doc2 пусто.
    assert client.get("/api/v1/clinician/reminders", headers=d2).json()["items"] == []
    assert len(client.get("/api/v1/clinician/reminders", headers=d1).json()["items"]) == 1

    db_session.execute(
        text("INSERT INTO escalations(id, patient_id, kind) VALUES (gen_random_uuid(), :p, 'SEVERE')"), {"p": p1}
    )
    db_session.commit()

    queue_d1 = client.get("/api/v1/escalations", headers=d1).json()["items"]
    queue_d2 = client.get("/api/v1/escalations", headers=d2).json()["items"]
    assert any(i["patient_id"] == p1 and i["in_roster"] for i in queue_d1)
    # Чужой сигнал не исчезает из общей очереди, но помечен вне ростера — UI
    # его не показывает как клинический случай doc2.
    assert all(i["patient_id"] != p2 for i in queue_d2)
    assert all(not i["in_roster"] for i in queue_d2)


def test_doc2_cannot_assign_screening_or_reminder_to_foreign_patient(client):
    _, p1, _, _, _, _, d2, _ = _setup(client)
    resp = client.post(
        f"/api/v1/clinician/patients/{p1}/assessments",
        json={"questionnaire_code": "HDRS-17", "answers": {"h1": 1}},
        headers=d2,
    )
    assert resp.status_code == 403 and resp.json()["code"] == "NO_ROSTER"

    due = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    resp = client.post(
        "/api/v1/clinician/reminders",
        json={"patient_id": p1, "kind": "call", "due_at": due},
        headers=d2,
    )
    assert resp.status_code == 403 and resp.json()["code"] == "NO_ROSTER"


def test_patient_cannot_use_clinician_panel_of_another_doctor(client):
    h1, p1, _, _, _, _, d2, _ = _setup(client)
    resp = client.get(f"/api/v1/clinician/patients/{p1}/report", headers=h1)
    assert resp.status_code == 403 and resp.json()["code"] == "ROLE_MISMATCH"
    resp = client.get("/api/v1/clinician/patients", headers=h1)
    assert resp.status_code == 403 and resp.json()["code"] == "ROLE_MISMATCH"
