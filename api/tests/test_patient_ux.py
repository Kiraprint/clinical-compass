"""FR-33/34: пациентский путь — скрытый пункт 9, суточный лимит автоответа,
справочник по своим назначениям, тренд чек-инов, данные личного кабинета.

Контракт — docs/04 FR-33/FR-34, docs/06 §3.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text

from app import db
from app.models import AuditLog, CheckinSession, Drug, PatientMedication, Prescription

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


def test_crisis_reply_ignores_daily_quota(client):
    """Лимит автоответа не прячет телефоны помощи: RED всегда получает HOTLINE."""
    h, _ = patient_ctx(client, "ux-chat-quota-red@test.ru")
    first = client.post("/api/v1/chat/thread", json={"content": "Добрый день"}, headers=h).json()
    assert first["reply"]["kind"] == "FAQ"

    finish_screening(client, h, {f"q{i}": 3 for i in range(1, 9)})  # SEVERE → RED

    second = client.post(
        "/api/v1/chat/thread", json={"content": "Совсем не справляюсь"}, headers=h
    ).json()
    assert second["reply"] is not None
    assert second["reply"]["kind"] == "HOTLINE"
    assert {r["phone"] for r in second["reply"]["help_resources"]} == {"8-800-2000-122", "112"}

    # Второй кризисный ответ тоже не режется: сообщение сохраняется без FAQ.
    third = client.post("/api/v1/chat/thread", json={"content": "Снова тяжело"}, headers=h).json()
    assert third["reply"]["kind"] == "HOTLINE"
    thread = client.get("/api/v1/chat/thread", headers=h).json()["items"]
    assert sum(1 for m in thread if m["sender"] == "SYSTEM" and m["kind"] == "HOTLINE") == 2
    assert sum(1 for m in thread if m["sender"] == "SYSTEM" and m["kind"] == "FAQ") == 1


def test_concurrent_chat_posts_yield_one_auto_reply(client):
    """Гонка двух POST: advisory-лок на (пациент, сутки МСК) даёт один автоответ."""
    h, pid = patient_ctx(client, "ux-chat-race@test.ru")
    from app.services import chat as chat_service

    async def _post_both():
        async def one(text: str):
            async with db.AsyncSessionLocal() as session:
                return await chat_service.post_message(session, uuid.UUID(pid), text)

        return await asyncio.gather(one("Раз"), one("Два"))

    results = asyncio.run(_post_both())
    assert sum(1 for r in results if r["reply"] is not None) == 1

    thread = client.get("/api/v1/chat/thread", headers=h).json()["items"]
    assert sum(1 for m in thread if m["sender"] == "SYSTEM") == 1


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
            patient_id=uuid.UUID(pid), title=drug.trade_name,
            dosage="50 мг", status="active", source="clinician", times_per_day=2,
        ))
        # Свободный текст врача («Авиандр 50 мг») точным совпадением не является:
        # подстрочный матч подставлял бы похожую карточку — карточки нет.
        s.add(PatientMedication(
            patient_id=uuid.UUID(pid), title=f"{drug.trade_name} 50 мг",
            dosage="50 мг", status="active", source="clinician",
        ))
        s.commit()

    items = client.get("/api/v1/me/drugs", headers=h).json()["items"]
    assert len(items) == 2
    by_title = {m["title"]: m for m in items}
    assert by_title[drug.trade_name]["status_label"] == "принимает"
    assert by_title[drug.trade_name]["drug"]["drug_id"] == drug.drug_id
    assert by_title[f"{drug.trade_name} 50 мг"]["drug"] is None

    # F6: чтение справочника по своим назначениям аудируется без названий препаратов.
    with db.SyncSessionLocal() as s:
        rows = s.execute(
            select(AuditLog).where(
                AuditLog.action == "READ_MY_DRUGS", AuditLog.actor_id == uuid.UUID(pid)
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].object_type == "patient"
    assert rows[0].object_id == pid


def test_checkin_history_returns_aggregate_without_raw_answers(client):
    h, pid = patient_ctx(client, "ux-checkin-hist@test.ru")
    assert client.get("/api/v1/checkins/history", headers=h).json() == {"items": []}
    # F6: агрегат самонаблюдения — PHI, чтение пишется в аудит без значений.
    with db.SyncSessionLocal() as s:
        assert len(s.execute(
            select(AuditLog).where(
                AuditLog.action == "READ_CHECKIN_HISTORY", AuditLog.actor_id == uuid.UUID(pid)
            )
        ).scalars().all()) == 1

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


def test_timeline_exposes_scale_max_total(client):
    """Верх шкалы для графика тренда приходит с сервера, а не угадывается в UI."""
    h, _ = patient_ctx(client, "ux-timeline-max@test.ru")
    finish_screening(client, h, PHQ8)
    runs = client.get("/api/v1/me/timeline", headers=h).json()["runs"]
    assert runs[-1]["questionnaire_code"] == "PHQ-9"
    assert runs[-1]["max_total"] == 24  # PHQ-8: 8 пунктов × 3


def test_prescribed_scale_completion_hides_banner(client):
    """Баннер «Назначен скрининг» гаснет после завершённой оценки по назначению."""
    h, pid = patient_ctx(client, "ux-banner@test.ru")
    with db.SyncSessionLocal() as s:
        s.add(Prescription(patient_id=uuid.UUID(pid), scale_code="PHQ-9"))
        s.commit()

    (before,) = client.get("/api/v1/me/prescribed-scales", headers=h).json()["scales"]
    assert before["code"] == "PHQ-9"
    assert before["completed_at"] is None

    finish_screening(client, h, PHQ8)

    (after,) = client.get("/api/v1/me/prescribed-scales", headers=h).json()["scales"]
    assert after["code"] == "PHQ-9"
    assert after["completed_at"] is not None


@pytest.mark.parametrize("days", [1, 365])
def test_checkin_history_window_bounds(client, days):
    h, _ = patient_ctx(client, f"ux-hist-{days}@test.ru")
    resp = client.get(f"/api/v1/checkins/history?days={days}", headers=h)
    assert resp.status_code == 200
