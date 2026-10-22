# FR-22: контакты пациента (телефон/дата рождения) — валидация, маскирование, гейты, аудит.
# Контракт: docs/04 §3.1; тесты — docs/06 §3.
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from app.models import AuditLog, User

from .conftest import admin_ctx, clinician_ctx, patient_ctx


def _pair(client, tag: str) -> tuple[dict, str, dict, str]:
    """Пациент с согласием + врач в roster. Возвращает (h_patient, pid, h_clinician, cid)."""
    h, pid = patient_ctx(client, f"pc{tag}@test.ru")
    c, cid = clinician_ctx(client, f"cl{tag}@test.ru")
    a, _ = admin_ctx(client, f"ad{tag}@test.ru")
    resp = client.post(
        "/api/v1/admin/roster",
        json={"patient_id": pid, "clinician_id": cid},
        headers=a,
    )
    assert resp.status_code == 201, resp.text
    return h, pid, c, cid


def test_clinician_sets_and_reads_contacts(client):
    _, pid, c, _ = _pair(client, "io")
    resp = client.patch(
        f"/api/v1/clinician/patients/{pid}/contacts",
        json={"phone": "+7 (926) 123-45-67", "birth_date": "1990-05-17"},
        headers=c,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["phone"] == "+79261234567"
    assert body["birth_date"] == "1990-05-17"
    assert body["age"] >= 18
    assert body["adult"] is True

    read = client.get(f"/api/v1/clinician/patients/{pid}/contacts", headers=c)
    assert read.status_code == 200, read.text
    assert read.json() == body


@pytest.mark.parametrize(
    "raw", ["89261234567", "79261234567", "9261234567", "+79261234567", " 8 (926) 123-45-67 "]
)
def test_phone_normalization(client, raw):
    _, pid, c, _ = _pair(client, "n" + uuid.uuid4().hex[:8])
    resp = client.patch(
        f"/api/v1/clinician/patients/{pid}/contacts", json={"phone": raw}, headers=c
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["phone"] == "+79261234567"


@pytest.mark.parametrize("raw", ["12345", "+14155552671", "не телефон", "899912345678", "+7 926 123-45-6a"])
def test_phone_invalid(client, raw):
    _, pid, c, _ = _pair(client, "b" + uuid.uuid4().hex[:8])
    resp = client.patch(
        f"/api/v1/clinician/patients/{pid}/contacts", json={"phone": raw}, headers=c
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "PHONE_INVALID"


def test_birth_date_bounds_and_minor_flag(client):
    _, pid, c, _ = _pair(client, "bd")

    future = (date.today() + timedelta(days=1)).isoformat()
    resp = client.patch(
        f"/api/v1/clinician/patients/{pid}/contacts", json={"birth_date": future}, headers=c
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "BIRTH_DATE_INVALID"

    resp = client.patch(
        f"/api/v1/clinician/patients/{pid}/contacts", json={"birth_date": "1899-12-31"}, headers=c
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "BIRTH_DATE_INVALID"

    resp = client.patch(
        f"/api/v1/clinician/patients/{pid}/contacts", json={"birth_date": "17.05.1990"}, headers=c
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "BIRTH_DATE_INVALID"

    # Несовершеннолетний хранится, но помечается adult=false (клинических запретов не вводим).
    minor = date.today().replace(year=date.today().year - 10).isoformat()
    resp = client.patch(
        f"/api/v1/clinician/patients/{pid}/contacts", json={"birth_date": minor}, headers=c
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["adult"] is False
    assert resp.json()["age"] == 10


def test_patient_list_and_card_mask_phone(client):
    _, pid, c, _ = _pair(client, "mask")
    assert client.patch(
        f"/api/v1/clinician/patients/{pid}/contacts",
        json={"phone": "89261234567", "birth_date": "1985-01-02"}, headers=c,
    ).status_code == 200

    rows = client.get("/api/v1/clinician/patients", headers=c).json()["items"]
    row = next(r for r in rows if r["patient_id"] == pid)
    assert row["phone"] == "+7•••123-45-67"
    assert "9261234567" not in row["phone"]
    assert row["birth_date"] == "1985-01-02"
    assert row["age"] >= 18
    # прежние ключи списка не сломаны
    assert {"email", "fio", "last_triage", "open_escalations"} <= set(row)

    card = client.get(f"/api/v1/clinician/patients/{pid}/report", headers=c).json()
    assert card["contacts"]["phone"] == "+7•••123-45-67"
    assert card["contacts"]["birth_date"] == "1985-01-02"


def test_rbac_contacts(client):
    h, pid, c, _ = _pair(client, "rbac")

    # Пациент не имеет доступа к врачебным endpoint'ам.
    assert client.get(f"/api/v1/clinician/patients/{pid}/contacts", headers=h).status_code == 403
    assert client.patch(
        f"/api/v1/clinician/patients/{pid}/contacts", json={"phone": "89261234567"}, headers=h
    ).status_code == 403

    # Врач вне roster — NO_ROSTER.
    other, _ = clinician_ctx(client, "other-rbac@test.ru")
    resp = client.get(f"/api/v1/clinician/patients/{pid}/contacts", headers=other)
    assert resp.status_code == 403
    assert resp.json()["code"] == "NO_ROSTER"
    resp = client.patch(
        f"/api/v1/clinician/patients/{pid}/contacts", json={"phone": "89261234567"}, headers=other
    )
    assert resp.status_code == 403

    # Врач не может пользоваться пациентским self-service.
    assert client.get("/api/v1/profile/contacts", headers=c).status_code == 403


def test_audit_rows_written(client, db_session):
    h, pid, c, _ = _pair(client, "audit")
    client.patch(
        f"/api/v1/clinician/patients/{pid}/contacts", json={"phone": "89261234567"}, headers=c
    )
    client.get(f"/api/v1/clinician/patients/{pid}/contacts", headers=c)
    client.get("/api/v1/profile/contacts", headers=h)
    client.patch("/api/v1/profile/contacts", json={"birth_date": "1991-03-04"}, headers=h)

    actions = {
        row.action
        for row in db_session.query(AuditLog).filter(AuditLog.object_id == pid).all()
    }
    # Профильные действия пишутся на object_id самого пользователя (тот же pid).
    assert {
        "UPDATE_PATIENT_CONTACTS",
        "READ_PATIENT_CONTACTS",
        "READ_OWN_CONTACTS",
        "UPDATE_OWN_CONTACTS",
    } <= actions

    # В аудите нет ПДн: только действие, тип и идентификатор объекта.
    rows = db_session.query(AuditLog).filter(AuditLog.action.like("%CONTACTS%")).all()
    assert rows
    for row in rows:
        assert row.object_type in {"patient", "user"}
        assert row.object_id == pid


def test_patient_self_update_only_own(client, db_session):
    h1, pid1 = patient_ctx(client, "self1@test.ru")
    _, pid2 = patient_ctx(client, "self2@test.ru")

    resp = client.patch(
        "/api/v1/profile/contacts",
        json={"phone": "8 926 111-22-33", "birth_date": "1993-07-08"},
        headers=h1,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["phone"] == "+79261112233"

    # Второй пациент не затронут.
    db_session.expire_all()
    other = db_session.get(User, uuid.UUID(pid2))
    assert other.phone is None and other.birth_date is None
    mine = db_session.get(User, uuid.UUID(pid1))
    assert mine.phone == "+79261112233"

    # Self-service не имеет маршрута с чужим patient_id: обновить другого нельзя.
    assert client.patch(
        f"/api/v1/profile/contacts/{pid2}", json={"phone": "89261112233"}, headers=h1
    ).status_code in (404, 405)


def test_consent_revocation_clears_contacts(client, db_session):
    h, pid, c, _ = _pair(client, "revoke")
    assert client.patch(
        f"/api/v1/clinician/patients/{pid}/contacts",
        json={"phone": "89261234567", "birth_date": "1990-05-17"}, headers=c,
    ).status_code == 200

    assert client.post("/api/v1/consent/revoke", headers=h).status_code == 204

    db_session.expire_all()
    user = db_session.get(User, uuid.UUID(pid))
    assert user.phone is None
    assert user.birth_date is None

    # 152-ФЗ: без согласия контакты недоступны ни пациенту, ни врачу.
    assert client.get("/api/v1/profile/contacts", headers=h).json()["code"] == "CONSENT_REQUIRED"
    assert client.get(
        f"/api/v1/clinician/patients/{pid}/contacts", headers=c
    ).json()["code"] == "CONSENT_REQUIRED"
