# Контракт: docs/04 §3.1; docs/06 §3 (checkin_once_per_day)
import pytest

from app import db

from .conftest import patient_ctx


def test_checkin_once_per_day(client):
    h, _ = patient_ctx(client, "cin@test.ru")
    assert client.post("/api/v1/checkins", json={"answers": {"q1": 1, "q2": 0, "q3": 1}}, headers=h).status_code == 201
    resp = client.post("/api/v1/checkins", json={"answers": {"q1": 0, "q2": 0, "q3": 0}}, headers=h)
    assert resp.status_code == 409
    assert resp.json()["code"] == "RUN_ALREADY_TODAY"


def test_checkin_today_returns_generated_items(client):
    h, _ = patient_ctx(client, "items@test.ru")
    resp = client.get("/api/v1/checkins/today", headers=h)
    assert resp.status_code == 200
    body = resp.json()
    assert body["done"] is False
    assert 2 <= len(body["items"]) <= 5
    for item in body["items"]:
        assert set(item) == {"key", "text", "max_value"}


def test_checkin_unknown_item_422(client):
    h, _ = patient_ctx(client, "cinbad@test.ru")
    resp = client.post("/api/v1/checkins", json={"answers": {"zzz": 1}}, headers=h)
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION"

def test_checkin_out_of_range_422(client):
    # Значение вне 0..max_value пункта → 422 (а не 500)
    h, _ = patient_ctx(client, "cinrange@test.ru")
    resp = client.post("/api/v1/checkins", json={"answers": {"q1": 5, "q2": 0, "q3": 0}}, headers=h)
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION"

def test_checkin_daily_unique_index(engine):
    # runs_checkin_daily: два CHECKIN за один день МСК недопустимы на уровне БД (TOCTOU-защита)
    from datetime import datetime, timezone
    from app.models import Run, User
    with db.SessionLocal() as s:
        user = User(email="cinidx@test.ru", password_hash="x", role="PATIENT")
        s.add(user)
        s.flush()
        s.add(Run(patient_id=user.id, questionnaire_code="PHQ-9", mode="CHECKIN",
                  finished_at=datetime.now(timezone.utc)))
        s.commit()
        with pytest.raises(Exception):
            s.add(Run(patient_id=user.id, questionnaire_code="PHQ-9", mode="CHECKIN",
                      started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc)))
            s.commit()
        s.rollback()