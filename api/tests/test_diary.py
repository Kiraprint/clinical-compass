"""Дневник: 5-точечная шкала настроения и совместимость с историчными 1–10 (docs/04 FR-9)."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app import db
from app.models import ClinicianRoster, DiaryEntry

from .conftest import clinician_ctx, patient_ctx


def test_mood_scale_new_entry_is_five_point(client):
    h, _ = patient_ctx(client, "dscale@test.ru")
    resp = client.post("/api/v1/diary", json={"content": "спокойно", "mood": 0}, headers=h)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["mood"] == 0
    assert body["mood_scale"] == "0-4"

    (listed,) = client.get("/api/v1/diary", headers=h).json()["items"]
    assert listed["mood_scale"] == "0-4"


def test_mood_out_of_five_point_range_422(client):
    h, _ = patient_ctx(client, "dscale_bad@test.ru")
    for bad in (5, -1):
        resp = client.post("/api/v1/diary", json={"content": "x", "mood": bad}, headers=h)
        assert resp.status_code == 422, bad


def test_legacy_mood_without_scale_is_not_reinterpreted(client, db_session):
    h, pid = patient_ctx(client, "dscale_legacy@test.ru")
    # Историчная строка: шкала 1–10, mood_scale NULL (как до миграции c6d7e8f9a0b1).
    legacy = DiaryEntry(patient_id=uuid.UUID(pid), content="старая запись", mood=7, mood_scale=None)
    db_session.add(legacy)
    db_session.commit()

    (item,) = client.get("/api/v1/diary", headers=h).json()["items"]
    assert item["mood"] == 7
    assert item["mood_scale"] == "1-10"

    ch, cid = clinician_ctx(client, "dscale_doc@test.ru")
    with db.SessionLocal() as s:
        s.add(ClinicianRoster(patient_id=uuid.UUID(pid), clinician_id=uuid.UUID(cid)))
        s.commit()
    report = client.get(f"/api/v1/clinician/patients/{pid}/report", headers=ch)
    assert report.status_code == 200, report.text
    (diary_item,) = [d for d in report.json()["diary"] if d["content"] == "старая запись"]
    assert diary_item["mood"] == 7
    assert diary_item["mood_scale"] == "1-10"

    # Новые записи рядом со старыми пишутся со своей шкалой.
    assert client.post("/api/v1/diary", json={"content": "новая", "mood": 3}, headers=h).status_code == 201
    with db.SessionLocal() as s:
        rows = {
            e.content: e.mood_scale
            for e in s.scalars(
                select(DiaryEntry).where(DiaryEntry.patient_id == uuid.UUID(pid))
            )
        }
    assert rows == {"старая запись": None, "новая": "0-4"}
