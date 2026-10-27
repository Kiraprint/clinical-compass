"""Расширенная карта пациента в ассистенте (FR-45, docs/07 §7.1).

Симптом: ассистент отвечал «препараты и анамнез не указаны» на пациента, у
которого они заполнены в карточке — `patient_context` отдавал только шкалы,
оценку, тренд, эскалации и дневник. Контракт: факты карточки доезжают до
промпта LLM и до детерминированного фолбэка, но ФИО остаётся маской, а
patient_id в промпт не попадает.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date, datetime, timezone

import pytest

from app import db
from app.config import settings
from app.models import (
    AdverseEvent,
    ChatInsight,
    DiaryEntry,
    PatientAnamnesis,
    PatientMedication,
    User,
    Visit,
)
from app.services import assistant_chat, clinician, llm_router

from .conftest import admin_ctx, clinician_ctx, patient_ctx

FIO = "Тестов Тест Тестович"
MED_TITLE = "Сертралин"
ANAMNESIS_FULL = (
    "Пациент Тестов Тест Тестович наблюдается с 2024 года, эпизод депрессии, "
    "ранее принимал флуоксетин."
)
AE_TEXT = "Головная боль после увеличения дозы"


def _grant(client, admin_h, patient_id, clinician_id) -> None:
    resp = client.post(
        "/api/v1/admin/roster",
        json={"patient_id": patient_id, "clinician_id": clinician_id},
        headers=admin_h,
    )
    assert resp.status_code == 201, resp.text


def _seed_card(patient_id: str, clinician_id: str, *, anamnesis: str = ANAMNESIS_FULL) -> None:
    """Заполняет ровно те сущности, которые видит врач в карточке."""
    pid, cid = uuid.UUID(patient_id), uuid.UUID(clinician_id)
    with db.SyncSessionLocal() as s:
        s.get(User, pid).fio = FIO
        s.add_all([
            PatientMedication(
                patient_id=pid, clinician_id=cid, title=MED_TITLE, dosage="50 мг",
                status="active", started_on=date(2026, 9, 1), times_per_day=2,
                notes="Утром и вечером, контроль АД", source="clinician",
            ),
            PatientMedication(
                patient_id=pid, clinician_id=cid, title="Флуоксетин", dosage="20 мг",
                status="stopped", started_on=date(2024, 1, 10),
                notes="Отменён из-за побочных эффектов", source="clinician",
            ),
            PatientAnamnesis(
                patient_id=pid, brief=anamnesis, created_by=cid, updated_by=cid,
            ),
            AdverseEvent(
                patient_id=pid, clinician_id=cid, event_text=AE_TEXT, severity="mild",
                onset_date=date(2026, 9, 20), suspected_drug=MED_TITLE,
                action_taken="monitoring", reported_by="clinician", status="confirmed",
            ),
            ChatInsight(
                patient_id=pid, kind="symptom", text="Жалуется на бессонницу",
                status="accepted", dedupe_hash=uuid.uuid4().hex + uuid.uuid4().hex[:32],
                created_by=cid, reviewed_by=cid, reviewed_at=datetime.now(timezone.utc),
            ),
            ChatInsight(
                patient_id=pid, kind="medication", text="Принимает препарат нерегулярно",
                status="pending", dedupe_hash=uuid.uuid4().hex + uuid.uuid4().hex[:32],
                created_by=cid,
            ),
            Visit(
                patient_id=pid, clinician_id=cid,
                visited_at=datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc),
                visit_type="in_person", duration_min=40,
                notes="Обсудили побочные эффекты",
            ),
        ])
        s.commit()


@pytest.fixture()
def card_patient(client):
    """Пациент с заполненной карточкой: (заголовки врача, clinician_id, patient_id)."""
    _, pid = patient_ctx(client, "pcf-card@test.ru")
    h, cid = clinician_ctx(client, "pcf-doc@test.ru")
    ah, _ = admin_ctx(client, "pcf-admin@test.ru")
    _grant(client, ah, pid, cid)
    _seed_card(pid, cid)
    return h, cid, pid


def _context(session, cid: str, pid: str) -> dict:
    """Сессию даёт фикстура `acall` — та же async-сессия, что в request-path."""
    return clinician.patient_context(session, cid, pid)


def test_patient_context_returns_card_facts(client, card_patient, acall):
    """Препараты, анамнез, НЯ, принятые факты чата и приёмы доезжают до контекста."""
    _, cid, pid = card_patient
    ctx = acall(_context, uuid.UUID(cid), uuid.UUID(pid))

    assert {m["title"] for m in ctx["medications"]} == {MED_TITLE, "Флуоксетин"}
    # active первым: модель не должна решать, какой препарат «сейчас».
    assert ctx["medications"][0]["title"] == MED_TITLE
    assert ctx["medications"][0]["status"] == "active"
    assert ctx["medications"][0]["times_per_day"] == 2
    assert ctx["anamnesis"]["brief"] == ANAMNESIS_FULL
    assert ctx["anamnesis"]["updated_at"]
    assert [e["event_text"] for e in ctx["adverse_events"]] == [AE_TEXT]
    assert ctx["adverse_events"][0]["severity"] == "mild"
    # Только принятые врачом факты (FR-31): pending не уходит в промпт.
    assert [i["text"] for i in ctx["chat_insights"]] == ["Жалуется на бессонницу"]
    assert [v["visit_type"] for v in ctx["visits"]] == ["in_person"]
    assert ctx["visits"][0]["notes"] == "Обсудили побочные эффекты"


def test_patient_context_without_facts_has_empty_sections(client, acall):
    """Без сущностей ключи присутствуют, но пустые — «нет в карте» вместо «нет данных»."""
    _, pid = patient_ctx(client, "pcf-empty@test.ru")
    h, cid = clinician_ctx(client, "pcf-empty-doc@test.ru")
    ah, _ = admin_ctx(client, "pcf-empty-admin@test.ru")
    _grant(client, ah, pid, cid)

    ctx = acall(_context, uuid.UUID(cid), uuid.UUID(pid))

    assert ctx["medications"] == []
    assert ctx["anamnesis"] is None
    assert ctx["adverse_events"] == []
    assert ctx["chat_insights"] == []
    assert ctx["visits"] == []


def test_card_budget_holds_with_long_diary(client, card_patient, acall):
    """6000-символьный потолок держится даже при трёх записях дневника по 4000 символов."""
    _, cid, pid = card_patient
    with db.SyncSessionLocal() as s:
        pid_uuid = uuid.UUID(pid)
        for _ in range(3):
            s.add(DiaryEntry(patient_id=pid_uuid, content="д" * 4000, mood=2, mood_scale="0-4"))
        s.commit()

    ctx = acall(_context, uuid.UUID(cid), uuid.UUID(pid))

    serialized = json.dumps(ctx, ensure_ascii=False, default=str)
    assert len(serialized) <= clinician.PATIENT_CONTEXT_CHARS
    # Факты карточки не вытесняются дневником: они сокращаются последними.
    assert ctx["medications"]
    assert ctx["anamnesis"]["brief"]
    assert all(len(d["content"]) <= clinician.PATIENT_CONTEXT_DIARY_CHARS for d in ctx["recent_diary"])


def test_full_card_keeps_fact_sections_in_prompt(client, card_patient, acall):
    """При полной карте (длинный дневник + все разделы) факты переживают обрезку строки."""
    _, cid, pid = card_patient
    with db.SyncSessionLocal() as s:
        pid_uuid = uuid.UUID(pid)
        for _ in range(3):
            s.add(DiaryEntry(patient_id=pid_uuid, content="д" * 4000, mood=2, mood_scale="0-4"))
        s.commit()

    ctx = acall(_context, uuid.UUID(cid), uuid.UUID(pid))
    line = llm_router._patient_line(1, ctx)

    assert len(line) <= llm_router.PATIENT_CARD_CHARS
    for section in ("препараты:", "анамнез:", "нежелательные реакции:", "факты из чата:", "визиты:"):
        assert section in line


def test_prompt_body_contains_facts_and_masks_fio(client, card_patient, acall, monkeypatch):
    """В теле запроса к LLM есть препарат и анамнез, ФИО — маска, patient_id нет."""
    _, cid, pid = card_patient
    monkeypatch.setattr(settings, "assistant_llm_url", "http://llm.test/v1")
    ctx = acall(_context, uuid.UUID(cid), uuid.UUID(pid))

    masked, _ = asyncio.run(assistant_chat._scrub_patient_contexts([ctx], [FIO]))
    body = llm_router._answer_body(
        [], "Что принимает пациент?", [{"text": "т", "source_title": "s"}], masked
    )
    prompt = body["messages"][1]["content"]

    assert MED_TITLE in prompt
    assert "анамнез:" in prompt
    assert "нежелательные реакции:" in prompt
    assert "факты из чата:" in prompt
    assert "визиты:" in prompt
    assert FIO not in prompt
    assert llm_router.PATIENT_MASK in prompt
    assert pid not in prompt
    # Новые свободные поля проходят деперсонализацию тем же scrub_text.
    assert "Тестович" not in prompt


def test_anamnesis_is_clipped_in_prompt(card_patient):
    """Анамнез 5000 символов не вытесняет остальные разделы карты."""
    _, _, pid = card_patient
    line = llm_router._patient_line(
        1, {"fio": FIO, "age": 34, "anamnesis": {"brief": "а" * 5000, "updated_at": "2026-09-01T10:00:00"}}
    )

    assert "анамнез: " in line
    assert len(line) <= llm_router.PATIENT_CARD_CHARS
    anamnesis_part = line.split("анамнез: ", 1)[1].split("; дневник:")[0]
    assert len(anamnesis_part) <= llm_router.PATIENT_ANAMNESIS_CHARS


def test_empty_sections_are_not_printed():
    """Пустые разделы не печатаются: «препараты: —» читалось бы как «препаратов нет»."""
    line = llm_router._patient_line(1, {"fio": FIO, "age": 34})

    for section in ("препараты:", "анамнез:", "нежелательные реакции:", "факты из чата:", "визиты:"):
        assert section not in line
