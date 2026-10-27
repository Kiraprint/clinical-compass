"""FR-37: анамнез пациента — вводные, черновик LLM, деперсонализация, сохранение.

Без сети: LLM перехватывается подменой `llm_router.chat_completion`.
Контракт: docs/04 FR-37, docs/10 R27/R23, docs/12.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from app.config import settings
from app.services import llm_router

from .conftest import admin_ctx, clinician_ctx, finish_screening, patient_ctx, run_async

PATIENT_NAME = "Иванова Мария Петровна"
CLINICIAN_NAME = "Смирнов Иван Иванович"

DRAFT = (
    "Жалобы\n- Тревожность.\n\n"
    "Анамнез заболевания\n- После COVID-19.\n\n"
    "Анамнез жизни\n- 34 года.\n\n"
    "Текущий статус\n- Утомляемость, нарушение сна."
)


def _pair(client, tag: str) -> tuple[dict, str, dict, str]:
    h, pid = patient_ctx(client, f"an{tag}@test.ru")
    c, cid = clinician_ctx(client, f"anc{tag}@test.ru")
    a, _ = admin_ctx(client, f"ana{tag}@test.ru")
    resp = client.post(
        "/api/v1/admin/roster", json={"patient_id": pid, "clinician_id": cid}, headers=a
    )
    assert resp.status_code == 201, resp.text
    return h, pid, c, cid


@pytest.fixture(autouse=True)
def _off_by_default(monkeypatch):
    monkeypatch.setattr(settings, "anamnesis_enabled", False)
    monkeypatch.setattr(settings, "assistant_llm_url", "")


def _enable(monkeypatch):
    monkeypatch.setattr(settings, "anamnesis_enabled", True)
    monkeypatch.setattr(settings, "assistant_llm_url", "http://llm:18080/v1")


def _fake_llm(monkeypatch, content: str = DRAFT, *, exc: Exception | None = None) -> dict:
    captured: dict = {}

    def fake(messages, *, temperature=0.0, timeout=None):
        captured["messages"] = messages
        captured["timeout"] = timeout
        if exc is not None:
            raise exc
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}

    monkeypatch.setattr(llm_router, "chat_completion", fake)
    return captured


def _url(pid: str, suffix: str = "") -> str:
    return f"/api/v1/clinician/patients/{pid}/anamnesis{suffix}"


def test_disabled_returns_503_without_llm(client, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("LLM must not be called when anamnesis is disabled")

    monkeypatch.setattr(llm_router, "chat_completion", boom)
    _, pid, c, _ = _pair(client, "off")
    assert client.get(_url(pid), headers=c).status_code == 503
    assert client.put(_url(pid), json={"brief": "x"}, headers=c).json()["code"] == "ANAMNESIS_DISABLED"
    resp = client.post(_url(pid, "/draft"), json={"brief": "x"}, headers=c)
    assert resp.status_code == 503 and resp.json()["code"] == "ANAMNESIS_DISABLED"


def test_put_get_roundtrip_report_and_audit(client, monkeypatch, db_session):
    _enable(monkeypatch)
    _, pid, c, cid = _pair(client, "io")

    empty = client.get(_url(pid), headers=c)
    assert empty.status_code == 200, empty.text
    assert empty.json() == {
        "patient_id": pid, "brief": None, "text": None, "updated_at": None, "updated_by": None
    }

    resp = client.put(
        _url(pid),
        json={"brief": "34 года, тревожность после COVID", "text": DRAFT},
        headers=c,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["brief"] == "34 года, тревожность после COVID"
    assert body["text"] == DRAFT
    assert body["updated_by"] == cid
    assert body["updated_at"]

    # Повторный PUT без text не затирает подтверждённый анамнез.
    again = client.put(_url(pid), json={"brief": "уточнено"}, headers=c)
    assert again.status_code == 200 and again.json()["text"] == DRAFT
    assert again.json()["brief"] == "уточнено"

    card = client.get(f"/api/v1/clinician/patients/{pid}/report", headers=c)
    assert card.status_code == 200, card.text
    assert card.json()["anamnesis"]["text"] == DRAFT

    # В покое оба поля — шифртекст (bytea), открытого текста в БД нет.
    raw = db_session.execute(
        text("select brief, text from patient_anamnesis where patient_id=:p"), {"p": pid}
    ).one()
    assert all(isinstance(v, (bytes, memoryview)) for v in raw)
    assert DRAFT.encode() not in bytes(raw[1])

    actions = db_session.execute(
        text("select action from audit_log where object_id=:p order by occurred_at"),
        {"p": pid},
    ).scalars().all()
    assert "ANAMNESIS_WRITE" in actions


def test_requires_active_consent_and_roster(client, monkeypatch):
    _enable(monkeypatch)
    h, pid, c, cid = _pair(client, "gate")

    client.post("/api/v1/consent/revoke", headers=h)
    assert client.get(_url(pid), headers=c).json()["code"] == "CONSENT_REQUIRED"
    assert client.put(_url(pid), json={"brief": "x"}, headers=c).json()["code"] == "CONSENT_REQUIRED"
    assert client.post(_url(pid, "/draft"), json={"brief": "x"}, headers=c).json()["code"] == "CONSENT_REQUIRED"

    # Чужой врач: 403 NO_ROSTER на все три ручки (гейт доступа — до флага/LLM).
    other, _ = clinician_ctx(client, "another@test.ru")
    for resp in (
        client.get(_url(pid), headers=other),
        client.put(_url(pid), json={"brief": "x"}, headers=other),
        client.post(_url(pid, "/draft"), json={"brief": "x"}, headers=other),
    ):
        assert resp.status_code == 403 and resp.json()["code"] == "NO_ROSTER"


def test_draft_masks_pii_and_does_not_save(client, monkeypatch, db_session):
    _enable(monkeypatch)
    captured = _fake_llm(monkeypatch)
    _, pid, c, cid = _pair(client, "pii")
    db_session.execute(text("update users set fio=:f where id=:i"), {"f": PATIENT_NAME, "i": pid})
    db_session.execute(text("update users set fio=:f where id=:i"), {"f": CLINICIAN_NAME, "i": cid})
    db_session.commit()

    brief = (
        f"{PATIENT_NAME}, телефон +7 916 123-45-67, дата рождения 01.02.1990. "
        "Тревожность и нарушение сна после COVID-19."
    )
    resp = client.post(_url(pid, "/draft"), json={"brief": brief}, headers=c)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["draft"] == DRAFT
    assert body["masked_report"]["applied"] is True
    assert body["masked_report"]["counts"]["FIO"] >= 1
    assert body["masked_report"]["counts"]["PHONE"] >= 1

    sent = json.dumps(captured["messages"], ensure_ascii=False)
    for raw in ("Иванова", "Мария", "Петровна", "Смирнов", "916", "1990"):
        assert raw not in sent
    assert "[ФИО]" in sent and "[ТЕЛЕФОН]" in sent and "[ДАТА РОЖДЕНИЯ]" in sent

    # Промпт задаёт четыре раздела и запрет на диагноз/лечение.
    system = captured["messages"][0]["content"]
    for section in ("Жалобы", "Анамнез заболевания", "Анамнез жизни", "Текущий статус"):
        assert section in system
    assert "Не ставь диагноз" in system

    # Черновик не сохранён: GET пуст, аудит — только факт запуска.
    assert client.get(_url(pid), headers=c).json()["text"] is None
    actions = db_session.execute(
        text("select action from audit_log where object_id=:p"), {"p": pid}
    ).scalars().all()
    assert "ANAMNESIS_DRAFT" in actions


def test_draft_uses_history_when_brief_empty(client, monkeypatch):
    _enable(monkeypatch)
    captured = _fake_llm(monkeypatch)
    h, pid, c, _ = _pair(client, "hist")
    result = finish_screening(client, h, {f"q{i}": 1 for i in range(1, 9)})
    assert result["total_score"] >= 4

    resp = client.post(_url(pid, "/draft"), json={}, headers=c)
    assert resp.status_code == 200, resp.text
    user_prompt = captured["messages"][1]["content"]
    assert "Скрининг" in user_prompt and str(result["total_score"]) in user_prompt


def test_draft_empty_source_409_and_llm_failure_503(client, monkeypatch):
    _enable(monkeypatch)
    called = {"n": 0}

    def fake(messages, **kw):
        called["n"] += 1
        return {"choices": [{"message": {"content": DRAFT}}]}

    monkeypatch.setattr(llm_router, "chat_completion", fake)
    _, pid, c, _ = _pair(client, "empty")
    resp = client.post(_url(pid, "/draft"), json={}, headers=c)
    assert resp.status_code == 409 and resp.json()["code"] == "ANAMNESIS_EMPTY"
    assert called["n"] == 0

    _fake_llm(monkeypatch, exc=RuntimeError("gateway down"))
    resp = client.post(_url(pid, "/draft"), json={"brief": "тревожность"}, headers=c)
    assert resp.status_code == 503 and resp.json()["code"] == "ANAMNESIS_UNAVAILABLE"


def test_prod_fail_closed_on_depersonalization(client, monkeypatch):
    """CC_PROD_MODE: недоступный ML-backend → 503, сырой текст в LLM не уходит."""
    _enable(monkeypatch)
    from app.services import depersonalize as dep

    monkeypatch.setattr(settings, "prod_mode", True)
    monkeypatch.setattr(settings, "depersonalize_ner", "remote")

    def unavailable(text, names=None):
        dep._backend_unavailable("remote", RuntimeError("sidecar down"))

    monkeypatch.setattr(dep, "_remote_spans", unavailable)
    called = {"n": 0}

    def fake(messages, **kw):
        called["n"] += 1
        return {"choices": [{"message": {"content": DRAFT}}]}

    monkeypatch.setattr(llm_router, "chat_completion", fake)
    _, pid, c, _ = _pair(client, "prod")
    resp = client.post(_url(pid, "/draft"), json={"brief": "тревожность"}, headers=c)
    assert resp.status_code == 503, resp.text
    assert resp.json()["code"] == "DEPERSONALIZATION_UNAVAILABLE"
    assert called["n"] == 0


def test_brief_length_limit(client, monkeypatch):
    _enable(monkeypatch)
    _, pid, c, _ = _pair(client, "len" + uuid.uuid4().hex[:4])
    resp = client.put(_url(pid), json={"brief": "x" * 4001}, headers=c)
    assert resp.status_code == 422


def test_consent_revoke_deletes_anamnesis(client, monkeypatch, db_session):
    """152-ФЗ: после отзыва согласия текст анамнеза удаляется из БД."""
    _enable(monkeypatch)
    h, pid, c, _ = _pair(client, "revoke")
    assert client.put(
        _url(pid), json={"brief": "тревожность", "text": DRAFT}, headers=c
    ).status_code == 200

    assert client.post("/api/v1/consent/revoke", headers=h).status_code == 204
    left = db_session.execute(
        text("select count(*) from patient_anamnesis where patient_id=:p"), {"p": pid}
    ).scalar()
    assert left == 0


def test_card_omits_anamnesis_row_without_active_consent(client, monkeypatch, db_session):
    """Строка анамнеза без активного согласия (аномалия данных) в карточку не попадает.

    Отзыв согласия удаляет анамнез, но карточка обязана не показывать спецкатегорию ПДн,
    даже если строка почему-то осталась: предикат `Consent.id IS NOT NULL` в джойне.
    """
    _enable(monkeypatch)
    _, pid, c, _ = _pair(client, "nocard")
    assert client.put(
        _url(pid), json={"brief": "тревожность", "text": DRAFT}, headers=c
    ).status_code == 200
    # Гасим согласие напрямую, строку анамнеза намеренно оставляем.
    db_session.execute(text("update consents set revoked_at = now() where user_id=:p"), {"p": pid})
    db_session.commit()

    from app.services import clinician as clinician_service

    card = run_async(clinician_service._patient_card, uuid.UUID(pid))
    assert card["anamnesis"] is None
    assert card["contacts"] is None
