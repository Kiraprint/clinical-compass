"""FR-25: админ-API ЕМИАС — RBAC, 503 без флага, аудит, витрина врача."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.config import settings
from app.emias import sync as emias_sync
from app.emias.sync import SyncStats
from app.models import AuditLog, ClinicianRoster, EmiasSyncRun

from .conftest import admin_ctx, clinician_ctx, patient_ctx

def _enable(monkeypatch):
    monkeypatch.setattr(settings, "emias_enabled", True)
    monkeypatch.setattr(settings, "emias_allow_insecure", True)
    monkeypatch.setattr(settings, "emias_base_url", "http://127.0.0.1:8090")

def _disable(monkeypatch):
    """Интеграция выключена независимо от ambient env (CC_EMIAS_ENABLED)."""
    monkeypatch.setattr(settings, "emias_enabled", False)
    monkeypatch.setattr(settings, "emias_allow_insecure", False)

def test_admin_endpoints_require_admin(client):
    clinician, _ = clinician_ctx(client, "c-emias@test.ru")
    patient, _ = patient_ctx(client, "p-emias-api@test.ru")
    for headers in (clinician, patient):
        assert client.get("/api/v1/admin/emias/health", headers=headers).status_code == 403
        assert client.post("/api/v1/admin/emias/sync", json={"mode": "patients"},
                           headers=headers).status_code == 403
        assert client.post("/api/v1/admin/emias/link", json={
            "patient_id": "00000000-0000-0000-0000-000000000001", "snils": "11223344595",
        }, headers=headers).status_code == 403

def test_health_reports_disabled_integration(client, monkeypatch):
    _disable(monkeypatch)
    admin, _ = admin_ctx(client, "a-emias@test.ru")
    resp = client.get("/api/v1/admin/emias/health", headers=admin)
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["last_run"] is None
    assert "secret" not in str(body).lower()

def test_sync_returns_503_when_disabled(client, db_session, monkeypatch):
    _disable(monkeypatch)
    admin, _ = admin_ctx(client, "a-emias-2@test.ru")
    resp = client.post("/api/v1/admin/emias/sync", json={"mode": "patients"}, headers=admin)
    assert resp.status_code == 503
    assert resp.json()["code"] == "EMIAS_DISABLED"
    assert db_session.scalar(select(AuditLog).where(AuditLog.action == "EMIA_SYNC")) is None
    assert db_session.scalar(select(EmiasSyncRun)) is None

def test_sync_runs_and_is_audited(client, db_session, monkeypatch):
    _enable(monkeypatch)
    admin, admin_id = admin_ctx(client, "a-emias-3@test.ru")
    monkeypatch.setattr(
        emias_sync, "sync_patients",
        lambda db, **kwargs: SyncStats(mode="patients", fetched=1, created=2),
    )

    resp = client.post("/api/v1/admin/emias/sync", json={"mode": "patients"}, headers=admin)
    assert resp.status_code == 202, resp.text
    run = resp.json()["run"]
    assert run["status"] == "ok"
    assert run["stats"]["created"] == 2
    assert run["mode"] == "patients"

    stored = db_session.scalar(select(EmiasSyncRun).where(EmiasSyncRun.id == uuid.UUID(run["id"])))
    assert stored is not None and stored.status == "ok"

    audit = db_session.scalar(select(AuditLog).where(AuditLog.action == "EMIA_SYNC"))
    assert audit is not None
    assert audit.actor_id == uuid.UUID(admin_id)

    runs = client.get("/api/v1/admin/emias/runs", headers=admin).json()["items"]
    assert runs and runs[0]["id"] == run["id"]

    health = client.get("/api/v1/admin/emias/health", headers=admin).json()
    assert health["enabled"] is True
    assert health["last_run"]["id"] == run["id"]

def test_sync_mode_one_requires_patient_id(client, monkeypatch):
    _enable(monkeypatch)
    admin, _ = admin_ctx(client, "a-emias-4@test.ru")
    resp = client.post("/api/v1/admin/emias/sync", json={"mode": "one"}, headers=admin)
    assert resp.status_code == 422
    assert resp.json()["code"] == "EMIAS_PATIENT_REQUIRED"

def test_failed_run_is_recorded_and_audited(client, db_session, monkeypatch):
    _enable(monkeypatch)
    admin, _ = admin_ctx(client, "a-emias-5@test.ru")

    def boom(db, **kwargs):
        raise emias_sync.EmiasError("network_error")

    monkeypatch.setattr(emias_sync, "sync_patients", boom)
    resp = client.post("/api/v1/admin/emias/sync", json={"mode": "patients"}, headers=admin)
    assert resp.status_code == 202
    assert resp.json()["run"]["status"] == "failed"
    assert resp.json()["run"]["stats"]["error_code"] == "network_error"
    assert db_session.scalar(select(AuditLog).where(AuditLog.action == "EMIA_SYNC")) is not None

def test_link_disabled_returns_503(client, monkeypatch):
    _disable(monkeypatch)
    admin, _ = admin_ctx(client, "a-emias-6@test.ru")
    resp = client.post("/api/v1/admin/emias/link", json={
        "patient_id": "00000000-0000-0000-0000-000000000001", "snils": "11223344595",
    }, headers=admin)
    assert resp.status_code == 503
    assert resp.json()["code"] == "EMIAS_DISABLED"

def test_clinician_emias_summary_requires_roster(client, db_session):
    clinician, clinician_id = clinician_ctx(client, "c-emias-2@test.ru")
    patient_headers, patient_id = patient_ctx(client, "p-emias-3@test.ru")

    path = f"/api/v1/clinician/patients/{patient_id}/emias"
    assert client.get(path, headers=clinician).status_code == 403

    db_session.add(ClinicianRoster(clinician_id=uuid.UUID(clinician_id), patient_id=uuid.UUID(patient_id)))
    db_session.commit()

    resp = client.get(path, headers=clinician)
    assert resp.status_code == 200
    body = resp.json()
    assert body["linked"] is False
    assert body["emias_patient_id"] is None
    assert body["medications"] == []
    assert body["encounters"] == 0

    audit = db_session.scalar(
        select(AuditLog).where(AuditLog.action == "READ_EMIAS", AuditLog.actor_id == uuid.UUID(clinician_id))
    )
    assert audit is not None

    # пациент не имеет доступа к карточке врача
    assert client.get(path, headers=patient_headers).status_code == 403
