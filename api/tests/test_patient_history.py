"""FR-23: единая лента истории пациента — порядок, фильтр типов, курсор, аудит."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models import AuditLog

from .conftest import admin_ctx, clinician_ctx, finish_screening, patient_ctx

PHQ_ALL = {f"q{i}": 0 for i in range(1, 10)}

def _setup(client):
    ph, pid = patient_ctx(client, "hist-p@test.ru")
    c, cid = clinician_ctx(client, "hist-c@test.ru")
    a, _ = admin_ctx(client, "hist-a@test.ru")
    assert client.post(
        "/api/v1/admin/roster", json={"patient_id": pid, "clinician_id": cid}, headers=a
    ).status_code == 201
    return c, cid, pid, ph

def _seed_events(client, c, pid, ph) -> None:
    finish_screening(client, ph, PHQ_ALL)
    assert client.post(
        f"/api/v1/clinician/patients/{pid}/visits",
        json={
            "visited_at": "2026-01-05T10:00:00+03:00",
            "visit_type": "online",
            "duration_min": 30,
            "notes": "Первичная беседа",
        },
        headers=c,
    ).status_code == 201
    assert client.post(
        f"/api/v1/clinician/patients/{pid}/medications",
        json={"title": "Сертралин", "dosage": "50 мг", "started_on": "2026-10-01"},
        headers=c,
    ).status_code == 201
    assert client.post(
        f"/api/v1/clinician/patients/{pid}/adverse-events",
        json={"event_text": "Тошнота после увеличения дозы", "severity": "mild",
              "onset_date": "2026-10-10", "suspected_drug": "Сертралин",
              "action_taken": "monitoring"},
        headers=c,
    ).status_code == 201

def test_history_merges_all_types_newest_first(client):
    c, _cid, pid, ph = _setup(client)
    _seed_events(client, c, pid, ph)

    body = client.get(f"/api/v1/clinician/patients/{pid}/history", headers=c).json()
    items = body["items"]
    types = {i["type"] for i in items}
    assert types == {"visit", "medication", "screening", "adverse"}
    ats = [i["at"] for i in items]
    assert ats == sorted(ats, reverse=True)
    # приём датирован январём — он самый старый в ленте
    assert items[-1]["type"] == "visit"
    for item in items:
        assert set(item) >= {"type", "at", "title", "summary", "ref_id", "source"}
        uuid.UUID(item["ref_id"])
    med = next(i for i in items if i["type"] == "medication")
    assert med["title"] == "Сертралин" and med["source"] == "clinician"
    screening = next(i for i in items if i["type"] == "screening")
    assert screening["source"] == "patient" and "PHQ-9" in screening["title"]

def test_history_types_filter(client):
    c, _cid, pid, ph = _setup(client)
    _seed_events(client, c, pid, ph)

    body = client.get(
        f"/api/v1/clinician/patients/{pid}/history?types=medication", headers=c
    ).json()
    assert [i["type"] for i in body["items"]] == ["medication"]

    bad = client.get(f"/api/v1/clinician/patients/{pid}/history?types=lab", headers=c)
    assert bad.status_code == 422

def test_history_limit_and_cursor(client):
    c, _cid, pid, ph = _setup(client)
    _seed_events(client, c, pid, ph)

    first = client.get(f"/api/v1/clinician/patients/{pid}/history?limit=2", headers=c).json()
    assert len(first["items"]) == 2 and first["next_before"]

    second = client.get(
        f"/api/v1/clinician/patients/{pid}/history?limit=2&before={first['next_before']}", headers=c
    ).json()
    assert len(second["items"]) >= 1
    assert {i["ref_id"] for i in first["items"]}.isdisjoint({i["ref_id"] for i in second["items"]})
    assert second["items"][0]["at"] <= first["items"][-1]["at"]

    assert client.get(
        f"/api/v1/clinician/patients/{pid}/history?before=garbage", headers=c
    ).status_code == 422

def test_history_audit_and_gating(client, db_session):
    c, _cid, pid, ph = _setup(client)
    _seed_events(client, c, pid, ph)
    client.get(f"/api/v1/clinician/patients/{pid}/history", headers=c)

    actions = db_session.scalars(select(AuditLog.action)).all()
    assert "READ_PATIENT_HISTORY" in actions

    stranger, _ = clinician_ctx(client, "hist-stranger@test.ru")
    denied = client.get(f"/api/v1/clinician/patients/{pid}/history", headers=stranger)
    assert denied.status_code == 403 and denied.json()["code"] == "NO_ROSTER"

    assert client.post("/api/v1/consent/revoke", headers=ph).status_code == 204
    revoked = client.get(f"/api/v1/clinician/patients/{pid}/history", headers=c)
    assert revoked.status_code == 403 and revoked.json()["code"] == "CONSENT_REQUIRED"
