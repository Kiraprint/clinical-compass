"""FR-33/34: пациентский путь — скрытый пункт 9, суточный лимит автоответа,
справочник по своим назначениям, тренд чек-инов, данные личного кабинета.

Контракт — docs/04 FR-33/FR-34, docs/06 §3.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text

from app import db
from app.models import CheckinSession, Drug, PatientMedication

from .conftest import finish_screening, patient_ctx

PHQ8 = {f"q{i}": 0 for i in range(1, 9)}  # q9 пациенту не предъявляется


def test_questionnaire_catalog_hides_item_9(client):
    h, _ = patient_ctx(client, "ux-cat@test.ru")
    phq = next(
        s for s in client.get("/api/v1/questionnaires", headers=h).json() if s["code"] == "PHQ-9"
    )
    assert [i["key"] for i in phq["items"]] == [f"q{i}" for i in range(1, 9)]
    assert all("умереть" not in i["text"] for i in phq["items"])


def test_screening_without_item_9_flags_risk_not_assessed(client):
    h, _ = patient_ctx(client, "ux-scr@test.ru")
    assessment = finish_screening(client, h, PHQ8)
    assert assessment["total_score"] == 0
    assert assessment["triage"] == "GREEN"
    assert assessment["omitted_items"] == ["q9"]
    assert {"kind": "NOT_ASSESSED", "item_key": "q9"} in assessment["risk_flags"]
    assert all(f["kind"] != "SUICIDAL_IDEATION" for f in assessment["risk_flags"])


def test_screening_scores_symptoms_over_remaining_items(client):
    """Сумма — по q1..q8: 8 × 3 = 24 (максимум неполной шкалы 24, а не 27)."""
    h, _ = patient_ctx(client, "ux-scr-max@test.ru")
    assessment = finish_screening(client, h, {f"q{i}": 3 for i in range(1, 9)})
    assert assessment["total_score"] == 24
    assert assessment["severity"] == "SEVERE"
    assert assessment["triage"] == "RED"
    assert assessment["omitted_items"] == ["q9"]


def test_screening_still_accepts_legacy_item_9_answer(client):
    """Старый клиент присылает q9 — ответ учитывается и поднимает RED (не теряем риск)."""
    h, _ = patient_ctx(client, "ux-scr-legacy@test.ru")
    assessment = finish_screening(client, h, {**PHQ8, "q9": 2})
    assert assessment["total_score"] == 2
    assert assessment["triage"] == "RED"
    assert assessment["omitted_items"] == []
    assert {"kind": "SUICIDAL_IDEATION", "item_key": "q9"} in assessment["risk_flags"]


def test_chat_auto_reply_once_per_moscow_day(client, db_session):
    h, pid = patient_ctx(client, "ux-chat@test.ru")

    first = client.post("/api/v1/chat/thread", json={"content": "Первый вопрос"}, headers=h)
    assert first.status_code == 201, first.text
    assert first.json()["reply"]["kind"] == "FAQ"

    second = client.post("/api/v1/chat/thread", json={"content": "Второй вопрос"}, headers=h)
    assert second.status_code == 201, second.text
    assert second.json()["reply"] is None

    thread = client.get("/api/v1/chat/thread", headers=h).json()["items"]
    assert len(thread) == 3
    assert sum(1 for m in thread if m["sender"] == "SYSTEM") == 1

    # Вчерашний автоответ не расходует лимит сегодняшнего дня (граница — МСК).
    db_session.execute(
        text("update chat_messages set created_at = created_at - interval '1 day' "
             "where patient_id = :pid and sender = 'SYSTEM'"),
        {"pid": uuid.UUID(pid)},
    )
    db_session.commit()
    third = client.post("/api/v1/chat/thread", json={"content": "Третий вопрос"}, headers=h)
    assert third.status_code == 201, third.text
    assert third.json()["reply"] is not None


def test_chat_hotline_reply_is_supportive_and_not_a_promise(client):
    h, _ = patient_ctx(client, "ux-chat-red@test.ru")
    finish_screening(client, h, {f"q{i}": 3 for i in range(1, 9)})  # SEVERE → RED
    reply = client.post("/api/v1/chat/thread", json={"content": "Совсем не справляюсь"}, headers=h).json()["reply"]
    assert reply["kind"] == "HOTLINE"
    text_body = reply["content"].lower()
    assert "специалисты уведомлены" not in text_body
    assert "требуется внимание" not in text_body
    assert "прямо сейчас" not in text_body
    assert reply["help_resources"]


def test_my_drug_reference_contains_only_own_medications(client):
    h, pid = patient_ctx(client, "ux-drugs@test.ru")
    with db.SyncSessionLocal() as s:
        drug = s.execute(select(Drug)).scalars().first()
    assert drug is not None, "справочник ГРЛС сеется перед каждым тестом"
    with db.SyncSessionLocal() as s:
        s.add(PatientMedication(
            patient_id=uuid.UUID(pid), title=f"{drug.trade_name} 50 мг",
            dosage="50 мг", status="active", source="clinician", times_per_day=2,
        ))
        s.commit()

    items = client.get("/api/v1/me/drugs", headers=h).json()["items"]
    assert len(items) == 1
    assert items[0]["status_label"] == "принимает"
    assert items[0]["drug"]["drug_id"] == drug.drug_id


def test_checkin_history_returns_aggregate_without_raw_answers(client):
    h, pid = patient_ctx(client, "ux-checkin-hist@test.ru")
    assert client.get("/api/v1/checkins/history", headers=h).json() == {"items": []}

    today = datetime.now(ZoneInfo("Europe/Moscow")).date()
    with db.SyncSessionLocal() as s:
        session = CheckinSession(
            patient_id=uuid.UUID(pid), checkin_date=today, rules_version="1.0.0",
            phase="done", status="completed", safety="none",
        )
        session.answers = {"MO1": 4, "TR1": 2, "SL1": 0, "IN1": 0, "EN1": 0, "FU1": 0, "HR4": 0}
        s.add(session)
        s.commit()

    items = client.get("/api/v1/checkins/history?days=7", headers=h).json()["items"]
    assert items == [{"date": today.isoformat(), "wellbeing": 1.0, "safety": "none"}]


def test_own_contacts_include_profile_fields_for_account(client):
    h, _ = patient_ctx(client, "ux-lk@test.ru")
    body = client.get("/api/v1/profile/contacts", headers=h).json()
    assert body["email"] == "ux-lk@test.ru"
    assert "fio" in body
    # Телефон/дата рождения правятся тем же endpoint'ом (FR-22), маска — в UI.
    patched = client.patch(
        "/api/v1/profile/contacts", json={"phone": "+7 916 123-45-67", "birth_date": "1990-05-06"}, headers=h
    ).json()
    assert patched["phone"] == "+79161234567"
    assert patched["birth_date"] == "1990-05-06"


@pytest.mark.parametrize("days", [1, 365])
def test_checkin_history_window_bounds(client, days):
    h, _ = patient_ctx(client, f"ux-hist-{days}@test.ru")
    resp = client.get(f"/api/v1/checkins/history?days={days}", headers=h)
    assert resp.status_code == 200
