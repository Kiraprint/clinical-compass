"""FR-31: факты из переписки с пациентом — черновики с подтверждением врача.

Без сети: фейковый /chat/completions через подмену urllib (как test_llm_router).
Контракт: docs/04 FR-31, docs/06 §3.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select, text

from app.config import settings
from app.models import ChatInsight

from .conftest import admin_ctx, clinician_ctx, patient_ctx

PATIENT_NAME = "Иванова Мария Петровна"
CLINICIAN_NAME = "Смирнов Иван Иванович"

FACTS = [
    {"kind": "sleep", "text": "Плохо спит последнюю неделю", "confidence": 0.82},
    {"kind": "medication", "text": "Пропускает вечерний приём препарата", "confidence": 0.67},
]


@pytest.fixture(autouse=True)
def _insights_off(monkeypatch):
    """По умолчанию фича выключена, как в проде без явного включения."""
    monkeypatch.setattr(settings, "chat_insights_enabled", False)
    monkeypatch.setattr(settings, "assistant_llm_url", "")


def _enable(monkeypatch):
    monkeypatch.setattr(settings, "chat_insights_enabled", True)
    monkeypatch.setattr(settings, "assistant_llm_url", "http://llm:18080/v1")
    monkeypatch.setattr(settings, "assistant_llm_api_key", "")


class _FakeResp:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_llm(monkeypatch, content: str | None = None, *, raise_exc: Exception | None = None):
    """Подменяет urlopen: фиксирует тело запроса и отдаёт content модели."""
    import urllib.request

    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        captured["timeout"] = timeout
        if raise_exc is not None:
            raise raise_exc
        return _FakeResp({"choices": [{"message": {"role": "assistant", "content": content}}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return captured


def _setup(client, email: str = "pi@test.ru", clinician_email: str = "ci@test.ru"):
    """Пациент с согласием + врач с ростером + пара сообщений пациента."""
    h, pid = patient_ctx(client, email)
    c, cid = clinician_ctx(client, clinician_email)
    a, _ = admin_ctx(client, f"a-{email}")
    resp = client.post(
        "/api/v1/admin/roster",
        json={"patient_id": pid, "clinician_id": cid},
        headers=a,
    )
    assert resp.status_code == 201, resp.text
    assert client.post(
        "/api/v1/chat/thread", json={"content": "Плохо сплю, вечером забываю таблетки"}, headers=h
    ).status_code == 201
    assert client.post(
        "/api/v1/chat/thread", json={"content": "Тревожно по вечерам, алкоголь не пью"}, headers=h
    ).status_code == 201
    return h, pid, c, cid


def _analyze(client, c, pid):
    return client.post(f"/api/v1/clinician/patients/{pid}/chat/analyze", headers=c)


def _items(client, c, pid, status: str | None = None):
    suffix = f"?status={status}" if status else ""
    resp = client.get(f"/api/v1/clinician/patients/{pid}/chat/insights{suffix}", headers=c)
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def test_analyze_creates_pending_encrypted_and_dedupes(client, monkeypatch, db_session):
    _enable(monkeypatch)
    _fake_llm(monkeypatch, json.dumps(FACTS))
    _, pid, c, _ = _setup(client)

    resp = _analyze(client, c, pid)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] == 2 and body["skipped"] == 0

    items = _items(client, c, pid, "pending")
    assert len(items) == 2
    assert {i["kind"] for i in items} == {"sleep", "medication"}
    assert all(i["status"] == "pending" for i in items)
    assert all(i["source_message_ids"] for i in items)
    # текст факта в БД — bytea (pgcrypto), не открытый text
    raw = db_session.execute(text("select text from chat_insights")).scalars().all()
    assert raw and all(isinstance(v, (bytes, memoryview)) for v in raw)

    # повторный запуск: те же факты → дедуп, новых строк нет
    again = _analyze(client, c, pid)
    assert again.status_code == 200, again.text
    assert again.json()["created"] == 0 and again.json()["skipped"] == 2
    assert db_session.scalar(select(ChatInsight.id).count() if False else text("select count(*) from chat_insights")) == 2


def test_prompt_masks_fio_and_limits(client, monkeypatch, db_session):
    _enable(monkeypatch)
    captured = _fake_llm(monkeypatch, json.dumps(FACTS))
    h, pid, c, cid = _setup(client, "pnm@test.ru", "cnm@test.ru")
    db_session.execute(text("update users set fio=:f where id=:i"), {"f": PATIENT_NAME, "i": pid})
    db_session.execute(text("update users set fio=:f where id=:i"), {"f": CLINICIAN_NAME, "i": cid})
    db_session.commit()
    assert client.post(
        "/api/v1/chat/thread",
        json={"content": f"Меня зовут {PATIENT_NAME}, телефон +7 916 123-45-67"},
        headers=h,
    ).status_code == 201

    resp = _analyze(client, c, pid)
    assert resp.status_code == 200, resp.text

    sent = json.dumps(captured["body"], ensure_ascii=False)
    assert "Иванова" not in sent and "Мария" not in sent
    assert "Смирнов" not in sent and "Иван Иванович" not in sent
    assert "916" not in sent  # структурные ПДн тоже маскируются
    assert "[ФИО]" in sent
    # промпт ограничивает до 10 фактов и 200 символов
    system = captured["body"]["messages"][0]["content"]
    assert "10" in system and "200" in system
    assert db_session.scalar(text("select count(*) from chat_insights")) == 2


def test_disabled_returns_503_without_llm_call(client, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("LLM must not be called when feature is disabled")

    import urllib.request
    monkeypatch.setattr(settings, "assistant_llm_url", "http://llm:18080/v1")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    _, pid, c, _ = _setup(client)

    resp = _analyze(client, c, pid)
    assert resp.status_code == 503
    assert resp.json()["code"] == "CHAT_INSIGHTS_DISABLED"
    assert "docs/17" in resp.json()["detail"]
    listed = client.get(f"/api/v1/clinician/patients/{pid}/chat/insights", headers=c)
    assert listed.status_code == 503
    assert listed.json()["code"] == "CHAT_INSIGHTS_DISABLED"


def test_llm_error_and_garbage_write_nothing(client, monkeypatch, db_session):
    import urllib.error

    _enable(monkeypatch)
    _, pid, c, _ = _setup(client)

    _fake_llm(monkeypatch, raise_exc=urllib.error.URLError("timeout"))
    resp = _analyze(client, c, pid)
    assert resp.status_code == 503
    assert resp.json()["code"] == "CHAT_INSIGHTS_UNAVAILABLE"

    _fake_llm(monkeypatch, "извините, не могу")
    resp = _analyze(client, c, pid)
    assert resp.status_code == 503
    assert resp.json()["code"] == "CHAT_INSIGHTS_UNAVAILABLE"

    assert db_session.scalar(text("select count(*) from chat_insights")) == 0


def test_llm_markdown_and_validation(client, monkeypatch):
    _enable(monkeypatch)
    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        "Вот факты:\n```json\n"
                        + json.dumps(
                            [
                                {"kind": "sleep", "text": "  Спит   по 4 часа  ", "confidence": 1},
                                {"kind": "bad_kind", "text": "мимо", "confidence": 0.5},
                                {"kind": "symptom", "text": "x" * 300, "confidence": 7},
                            ],
                            ensure_ascii=False,
                        )
                        + "\n```\nГотово."
                    )
                }
            }
        ]
    }
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResp(payload))
    _, pid, c, _ = _setup(client)

    resp = _analyze(client, c, pid)
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] == 2  # невалидный kind отброшен
    items = _items(client, c, pid)
    by_kind = {i["kind"]: i for i in items}
    assert by_kind["sleep"]["text"] == "Спит по 4 часа"
    assert len(by_kind["symptom"]["text"]) == 200
    assert by_kind["symptom"]["confidence"] is None  # вне [0,1] → null


def test_empty_dialogue_409(client, monkeypatch):
    _enable(monkeypatch)
    captured = _fake_llm(monkeypatch, "[]")
    _, pid, c, _ = _setup(client, "pe@test.ru", "ce@test.ru")
    # Чистим переписку: анализировать нечего.
    from app import db as app_db

    with app_db.SessionLocal() as s:
        s.execute(text("delete from chat_messages where patient_id=:p"), {"p": pid})
        s.commit()

    resp = _analyze(client, c, pid)
    assert resp.status_code == 409
    assert resp.json()["code"] == "CHAT_INSIGHTS_EMPTY"
    assert captured == {}


def test_accept_in_report_reject_not_and_404(client, monkeypatch):
    _enable(monkeypatch)
    _fake_llm(monkeypatch, json.dumps(FACTS))
    _, pid, c, _ = _setup(client, "pa@test.ru", "ca@test.ru")
    assert _analyze(client, c, pid).status_code == 200
    items = _items(client, c, pid, "pending")
    keep = next(i for i in items if i["kind"] == "sleep")
    drop = next(i for i in items if i["kind"] == "medication")

    resp = client.patch(
        f"/api/v1/clinician/patients/{pid}/chat/insights/{keep['id']}",
        json={"status": "accepted"}, headers=c,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "accepted"
    assert resp.json()["reviewed_at"]
    # идемпотентность: повторное решение не меняет reviewed_at
    reviewed_at = resp.json()["reviewed_at"]
    again = client.patch(
        f"/api/v1/clinician/patients/{pid}/chat/insights/{keep['id']}",
        json={"status": "accepted"}, headers=c,
    )
    assert again.status_code == 200 and again.json()["reviewed_at"] == reviewed_at

    resp = client.patch(
        f"/api/v1/clinician/patients/{pid}/chat/insights/{drop['id']}",
        json={"status": "rejected"}, headers=c,
    )
    assert resp.status_code == 200 and resp.json()["status"] == "rejected"

    assert [i["id"] for i in _items(client, c, pid, "pending")] == []
    report = client.get(f"/api/v1/clinician/patients/{pid}/report", headers=c).json()
    assert [i["text"] for i in report["chat_insights"]] == ["Плохо спит последнюю неделю"]
    assert "source_message_ids" not in report["chat_insights"][0]

    missing = client.patch(
        f"/api/v1/clinician/patients/{pid}/chat/insights/00000000-0000-0000-0000-000000000000",
        json={"status": "accepted"}, headers=c,
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "INSIGHT_NOT_FOUND"


def test_foreign_patient_and_insight_404(client, monkeypatch):
    _enable(monkeypatch)
    _fake_llm(monkeypatch, json.dumps(FACTS))
    _, pid, c, _ = _setup(client, "px@test.ru", "cx@test.ru")
    assert _analyze(client, c, pid).status_code == 200
    insight = _items(client, c, pid)[0]

    other, _ = clinician_ctx(client, "co@test.ru")
    resp = _analyze(client, other, pid)
    assert resp.status_code == 403 and resp.json()["code"] == "NO_ROSTER"
    # чужой врач не может решить факт: сначала roster-гейт → 403
    resp = client.patch(
        f"/api/v1/clinician/patients/{pid}/chat/insights/{insight['id']}",
        json={"status": "accepted"}, headers=other,
    )
    assert resp.status_code == 403

    # Врач пациента А не может достучаться до факта пациента Б через свой ростер.
    _, pid_b = patient_ctx(client, "pb@test.ru")
    c2, cid2 = clinician_ctx(client, "c2@test.ru")
    admin, _ = admin_ctx(client, "a2@test.ru")
    assert client.post(
        "/api/v1/admin/roster", json={"patient_id": pid_b, "clinician_id": cid2}, headers=admin
    ).status_code == 201
    resp = client.patch(
        f"/api/v1/clinician/patients/{pid_b}/chat/insights/{insight['id']}",
        json={"status": "accepted"}, headers=c2,
    )
    assert resp.status_code == 404 and resp.json()["code"] == "INSIGHT_NOT_FOUND"


def test_patient_role_forbidden(client, monkeypatch):
    _enable(monkeypatch)
    _fake_llm(monkeypatch, json.dumps(FACTS))
    h, pid, c, _ = _setup(client, "pp@test.ru", "cp@test.ru")

    assert _analyze(client, h, pid).status_code == 403
    assert client.get(f"/api/v1/clinician/patients/{pid}/chat/insights", headers=h).status_code == 403
    assert client.patch(
        f"/api/v1/clinician/patients/{pid}/chat/insights/00000000-0000-0000-0000-000000000000",
        json={"status": "accepted"}, headers=h,
    ).status_code == 403


def test_consent_revoke_deletes_insights(client, monkeypatch, db_session):
    _enable(monkeypatch)
    _fake_llm(monkeypatch, json.dumps(FACTS))
    h, pid, c, _ = _setup(client, "pr@test.ru", "cr@test.ru")
    assert _analyze(client, c, pid).status_code == 200
    assert db_session.scalar(text("select count(*) from chat_insights")) == 2

    assert client.post("/api/v1/consent/revoke", headers=h).status_code == 204
    assert db_session.scalar(text("select count(*) from chat_insights")) == 0
