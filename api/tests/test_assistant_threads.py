# FR-21: сессии клинического ассистента — persistent-контекст и мультипациент.
# Контракт: docs/04 §3.1, docs/05 E9; тесты — docs/06 §3.
"""Треды ассистента: изоляция врачей, roster-гейт, история, аудит, каскад."""
from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.config import settings

_TS = datetime(2026, 1, 1, tzinfo=UTC)

qdrant = pytest.importorskip("qdrant_client")

def _qdrant_available() -> bool:
    try:
        from qdrant_client import QdrantClient

        QdrantClient(url=settings.qdrant_url, timeout=2).get_collections()
        return True
    except Exception:
        return False

pytestmark = pytest.mark.skipif(not _qdrant_available(), reason="Qdrant недоступен (CC_QDRANT_URL)")

@pytest.fixture(scope="module")
def kb_collection():
    """Индексирует тестовую коллекцию и убирает её после модуля."""
    from qdrant_client import QdrantClient

    from app.kb_seed import build_corpus
    from app.services import kb
    from app.services.embeddings import HashEmbedder

    client = QdrantClient(url=settings.qdrant_url)
    if client.collection_exists(kb.COLLECTION):
        client.delete_collection(kb.COLLECTION)
    kb.upsert_documents(client, HashEmbedder(), build_corpus())
    yield
    client.delete_collection(kb.COLLECTION)

from .conftest import admin_ctx, clinician_ctx, patient_ctx  # noqa: E402

def _grant(client, admin_h, patient_id: str, clinician_id: str) -> None:
    resp = client.post(
        "/api/v1/admin/roster",
        json={"patient_id": patient_id, "clinician_id": clinician_id},
        headers=admin_h,
    )
    assert resp.status_code == 201, resp.text

def _setup(client, email: str = "th-c@test.ru"):
    """Врач + 2 пациента в roster + админ. Возвращает (headers, patient ids)."""
    ch, cid = clinician_ctx(client, email)
    ah, _ = admin_ctx(client, f"adm-{email}")
    _, p1 = patient_ctx(client, f"p1-{email}")
    _, p2 = patient_ctx(client, f"p2-{email}")
    _grant(client, ah, p1, cid)
    _grant(client, ah, p2, cid)
    return ch, [p1, p2]

def test_thread_create_message_and_read_back(client, kb_collection):
    """Создание треда с 2 пациентами → вопрос → история с цитатами персистится."""
    ch, (p1, p2) = _setup(client)
    resp = client.post(
        "/api/v1/assistant/threads",
        json={"title": "Разбор случая", "patient_ids": [p1, p2, p1]},
        headers=ch,
    )
    assert resp.status_code == 201, resp.text
    thread = resp.json()
    assert thread["title"] == "Разбор случая"
    assert thread["patient_ids"] == [p1, p2], "дубликаты пациентов дедуплицируются"

    tid = thread["id"]
    resp = client.post(
        f"/api/v1/assistant/threads/{tid}/messages",
        json={"content": "Какой порог тяжести у шкалы PHQ-9?"},
        headers=ch,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"] and body["citations"]
    assert len(body["message_ids"]) == 2
    assert body["skipped_patient_ids"] == []
    patient_cites = [c for c in body["citations"] if c["topic"] == "ПАЦИЕНТ"]
    assert len(patient_cites) == 2, "в контекст попали карты обоих пациентов"

    detail = client.get(f"/api/v1/assistant/threads/{tid}", headers=ch)
    assert detail.status_code == 200, detail.text
    messages = detail.json()["messages"]
    assert [m["role"] for m in messages] == ["CLINICIAN", "ASSISTANT"]
    assert messages[0]["content"] == "Какой порог тяжести у шкалы PHQ-9?"
    assert messages[1]["citations"] == body["citations"]
    assert messages[1]["id"] == body["message_ids"][1]

    listed = client.get("/api/v1/assistant/threads", headers=ch).json()["items"]
    assert len(listed) == 1
    assert listed[0]["id"] == tid
    assert listed[0]["message_count"] == 2
    assert listed[0]["patient_ids"] == [p1, p2]

def test_foreign_thread_is_404(client, kb_collection):
    """R5/IDOR: врач B не видит и не меняет тред врача A (404, без утечки)."""
    ch_a, (p1, _) = _setup(client, "th-a@test.ru")
    ch_b, _ = clinician_ctx(client, "th-b@test.ru")
    tid = client.post(
        "/api/v1/assistant/threads", json={"patient_ids": [p1]}, headers=ch_a
    ).json()["id"]

    assert client.get(f"/api/v1/assistant/threads/{tid}", headers=ch_b).status_code == 404
    assert (
        client.get(f"/api/v1/assistant/threads/{tid}", headers=ch_b).json()["code"]
        == "THREAD_NOT_FOUND"
    )
    assert (
        client.patch(f"/api/v1/assistant/threads/{tid}", json={"title": "x"}, headers=ch_b).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/assistant/threads/{tid}/messages", json={"content": "?"}, headers=ch_b
        ).status_code
        == 404
    )
    assert client.delete(f"/api/v1/assistant/threads/{tid}", headers=ch_b).status_code == 404
    # Свой список пуст — чужой тред не просачивается.
    assert client.get("/api/v1/assistant/threads", headers=ch_b).json()["items"] == []
    # Владелец по-прежнему видит тред.
    assert client.get(f"/api/v1/assistant/threads/{tid}", headers=ch_a).status_code == 200

def test_patient_ids_roster_gate(client, kb_collection):
    """Не roster-пациент → 403 PATIENT_NOT_IN_ROSTER (создание и сообщение)."""
    ch, (p1, _) = _setup(client, "th-roster@test.ru")
    _, stranger = patient_ctx(client, "stranger@test.ru")

    resp = client.post(
        "/api/v1/assistant/threads", json={"patient_ids": [stranger]}, headers=ch
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "PATIENT_NOT_IN_ROSTER"

    tid = client.post(
        "/api/v1/assistant/threads", json={"patient_ids": [p1]}, headers=ch
    ).json()["id"]
    resp = client.post(
        f"/api/v1/assistant/threads/{tid}/messages",
        json={"content": "Какой порог PHQ-9?", "patient_ids": [p1, stranger]},
        headers=ch,
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "PATIENT_NOT_IN_ROSTER"

def test_patient_limit(client, kb_collection):
    """≤10 пациентов в треде."""
    ch, _ = _setup(client, "th-limit@test.ru")
    too_many = [str(uuid.uuid4()) for _ in range(11)]
    resp = client.post(
        "/api/v1/assistant/threads", json={"patient_ids": too_many}, headers=ch
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "TOO_MANY_PATIENTS"

def test_patch_replaces_patients_and_title(client, kb_collection):
    ch, (p1, p2) = _setup(client, "th-patch@test.ru")
    tid = client.post(
        "/api/v1/assistant/threads", json={"patient_ids": [p1]}, headers=ch
    ).json()["id"]
    resp = client.patch(
        f"/api/v1/assistant/threads/{tid}",
        json={"title": "Новый заголовок", "patient_ids": [p2]},
        headers=ch,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "Новый заголовок"
    assert resp.json()["patient_ids"] == [p2]

def test_read_thread_with_patients_is_audited(client, db_session, kb_collection):
    """Чтение треда с пациентами → append-only audit READ_ASSISTANT_THREAD."""
    from sqlalchemy import select

    from app.models import AuditLog

    ch, (p1, _) = _setup(client, "th-audit@test.ru")
    tid = client.post(
        "/api/v1/assistant/threads", json={"patient_ids": [p1]}, headers=ch
    ).json()["id"]
    assert client.get(f"/api/v1/assistant/threads/{tid}", headers=ch).status_code == 200

    rows = db_session.scalars(
        select(AuditLog).where(
            AuditLog.action == "READ_ASSISTANT_THREAD", AuditLog.object_id == tid
        )
    ).all()
    assert len(rows) == 1

def test_delete_thread_cascades(client, db_session, kb_collection):
    """DELETE треда → сообщения и patient-привязки удалены каскадом."""
    from sqlalchemy import func, select

    from app.models import AssistantMessage, AssistantThreadPatient

    ch, (p1, _) = _setup(client, "th-del@test.ru")
    tid = client.post(
        "/api/v1/assistant/threads", json={"patient_ids": [p1]}, headers=ch
    ).json()["id"]
    assert (
        client.post(
            f"/api/v1/assistant/threads/{tid}/messages",
            json={"content": "Какой порог PHQ-9?"},
            headers=ch,
        ).status_code
        == 200
    )
    assert client.delete(f"/api/v1/assistant/threads/{tid}", headers=ch).status_code == 204
    assert client.get(f"/api/v1/assistant/threads/{tid}", headers=ch).status_code == 404

    thread_uuid = uuid.UUID(tid)
    db_session.expire_all()
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(AssistantMessage)
            .where(AssistantMessage.thread_id == thread_uuid)
        )
        == 0
    )
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(AssistantThreadPatient)
            .where(AssistantThreadPatient.thread_id == thread_uuid)
        )
        == 0
    )

def test_thread_without_patients_answers_from_kb(client, kb_collection):
    """Тред без пациентов: обычный справочный ответ, skipped пуст."""
    ch, _ = _setup(client, "th-kbonly@test.ru")
    tid = client.post("/api/v1/assistant/threads", json={}, headers=ch).json()["id"]
    resp = client.post(
        f"/api/v1/assistant/threads/{tid}/messages",
        json={"content": "триаж RED"},
        headers=ch,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"]
    assert body["skipped_patient_ids"] == []
    assert all(c["topic"] != "ПАЦИЕНТ" for c in body["citations"])

def test_redline_in_history_does_not_poison_next_turn(client, kb_collection):
    """Red line проверяется по текущему вопросу: redline-реплика в истории не
    должна глушить retrieval следующих ходов треда."""
    ch, (p1, _) = _setup(client, "th-redline@test.ru")
    tid = client.post(
        "/api/v1/assistant/threads", json={"patient_ids": [p1]}, headers=ch
    ).json()["id"]
    url = f"/api/v1/assistant/threads/{tid}/messages"

    first = client.post(url, json={"content": "Поставь диагноз депрессии"}, headers=ch)
    assert first.status_code == 200, first.text
    assert first.json()["refused"] is True
    assert first.json()["refusal_code"] == "DIAGNOSIS_REDLINE"

    second = client.post(url, json={"content": "Какой порог тяжести у шкалы PHQ-9?"}, headers=ch)
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["refused"] is False and body["grounded"] is True
    assert any("PHQ-9" in c["source_title"] for c in body["citations"]), body["citations"]

def test_revoked_consent_skips_patient_card(client, db_session, kb_collection):
    """Потеря согласия после создания треда: пациент попадает в skipped, его
    карта не цитируется, а PHI-доступ к истории всё равно аудируется."""
    from sqlalchemy import select

    from app.models import AuditLog

    ch, cid = clinician_ctx(client, "th-skip@test.ru")
    ah, _ = admin_ctx(client, "adm-th-skip@test.ru")
    ph1, p1 = patient_ctx(client, "skip-p1@test.ru")
    _, p2 = patient_ctx(client, "skip-p2@test.ru")
    _grant(client, ah, p1, cid)
    _grant(client, ah, p2, cid)
    tid = client.post(
        "/api/v1/assistant/threads", json={"patient_ids": [p1, p2]}, headers=ch
    ).json()["id"]

    assert client.post("/api/v1/consent/revoke", headers=ph1).status_code == 204
    resp = client.post(
        f"/api/v1/assistant/threads/{tid}/messages",
        json={"content": "Какой порог тяжести у шкалы PHQ-9?"},
        headers=ch,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["skipped_patient_ids"] == [p1]
    cited = {c["guideline_id"] for c in body["citations"]}
    assert f"patient:{p1}" not in cited
    assert f"patient:{p2}" in cited

    rows = db_session.scalars(
        select(AuditLog).where(
            AuditLog.action == "READ_ASSISTANT_THREAD", AuditLog.object_id == tid
        )
    ).all()
    assert len(rows) == 1

class _LLMResp:
    """Фейковый ответ /chat/completions (urllib-совместимый)."""

    def __init__(self, body: dict):
        self._body = body

    def read(self) -> bytes:
        import json

        return json.dumps(self._body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

def _fake_llm(monkeypatch, reply: str, *, raise_exc: Exception | None = None) -> list[dict]:
    """Включает LLM-шлюз и подменяет сеть. Возвращает тела ушедших запросов."""
    import json
    import urllib.request

    monkeypatch.setattr(settings, "assistant_llm_url", "http://llm.test/v1")
    bodies: list[dict] = []

    def fake_urlopen(req, timeout=None):
        bodies.append(json.loads(req.data))
        if raise_exc is not None:
            raise raise_exc
        return _LLMResp({"choices": [{"message": {"role": "assistant", "content": reply}}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return bodies

class _SpyAgent:
    """Агент-заглушка: запоминает, что реально ушло в RAG."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def invoke(self, query, **kwargs):
        self.calls.append((query, kwargs))
        return {"answer": "ответ", "citations": [], "topic": "ЛЕКАРСТВА"}

def _new_thread(client, email: str) -> tuple[dict, str]:
    """Врач + тред с 2 roster-пациентами; возвращает (headers, url сообщений)."""
    ch, ids = _setup(client, email)
    tid = client.post(
        "/api/v1/assistant/threads", json={"patient_ids": ids}, headers=ch
    ).json()["id"]
    return ch, f"/api/v1/assistant/threads/{tid}/messages"

def test_followup_question_rewritten_for_retrieval(client, monkeypatch):
    """Уточняющий вопрос ищется по LLM-переформулировке; в агент уходят оба
    текста: исходный (red line/ответ) и retrieval_query (поиск и раздел)."""
    from app.services import assistant_chat

    ch, url = _new_thread(client, "th-followup@test.ru")
    llm = _fake_llm(monkeypatch, "Какие дозы препарата Авиандр (маритупирдин)?")
    spy = _SpyAgent()
    monkeypatch.setattr(assistant_chat, "shared_factory", lambda: spy)

    first = client.post(url, json={"content": "Что известно про Авиандр при депрессии?"}, headers=ch).json()
    assert first["rewritten"] is False
    assert first["retrieval_query"] == "Что известно про Авиандр при депрессии?"
    assert llm == [], "одиночный вопрос не тратит LLM-вызов"

    second = client.post(url, json={"content": "а в каких дозах?"}, headers=ch).json()
    assert second["rewritten"] is True
    assert second["retrieval_query"] == "Какие дозы препарата Авиандр (маритупирдин)?"
    query, kwargs = spy.calls[-1]
    assert query == "а в каких дозах?"  # ответ/red line — по оригиналу
    assert kwargs["retrieval_query"] == second["retrieval_query"]
    # Переформулировка идёт по истории треда, а не по конкатенации в embedding.
    assert "Что известно про Авиандр" in llm[0]["messages"][1]["content"]
    assert llm[0]["temperature"] == 0.0

def test_followup_without_llm_uses_current_question(client, monkeypatch):
    """LLM выключен → поиск по текущему вопросу, rewritten=False, сеть не трогаем."""
    import urllib.request

    from app.services import assistant_chat

    monkeypatch.setattr(settings, "assistant_llm_url", "")
    monkeypatch.setattr(settings, "assistant_llm_api_key", "")

    def _no_network(*args, **kwargs):
        raise AssertionError("LLM выключен — сетевых вызовов быть не должно")

    monkeypatch.setattr(urllib.request, "urlopen", _no_network)
    ch, url = _new_thread(client, "th-followup-off@test.ru")
    spy = _SpyAgent()
    monkeypatch.setattr(assistant_chat, "shared_factory", lambda: spy)

    client.post(url, json={"content": "Что известно про Авиандр при депрессии?"}, headers=ch)
    body = client.post(url, json={"content": "а в каких дозах?"}, headers=ch).json()

    assert body["rewritten"] is False
    assert body["retrieval_query"] == "а в каких дозах?"
    assert spy.calls[-1][1]["retrieval_query"] is None
    assert spy.calls[-1][0] == "а в каких дозах?"

@pytest.mark.parametrize(
    "reply,raise_exc",
    [
        ("", None),  # пустой ответ модели
        ("   \n  ", None),  # мусор из пробелов
        (None, "URLError"),  # шлюз недоступен
        (None, "TimeoutError"),  # таймаут
    ],
)
def test_followup_llm_failure_falls_back_to_original(client, monkeypatch, reply, raise_exc):
    """Ошибка/пустой ответ LLM → исходный вопрос, ответ не ломается."""
    import urllib.error

    from app.services import assistant_chat

    exc = None
    if raise_exc == "URLError":
        exc = urllib.error.URLError("down")
    elif raise_exc == "TimeoutError":
        exc = TimeoutError("timeout")
    llm = _fake_llm(monkeypatch, reply or "", raise_exc=exc)
    ch, url = _new_thread(client, f"th-followup-fail-{raise_exc}-{bool(reply)}@test.ru")
    spy = _SpyAgent()
    monkeypatch.setattr(assistant_chat, "shared_factory", lambda: spy)

    client.post(url, json={"content": "Что известно про Авиандр при депрессии?"}, headers=ch)
    body = client.post(url, json={"content": "а в каких дозах?"}, headers=ch).json()

    assert len(llm) == 1, "переформулировка пыталась вызвать LLM"
    assert body["rewritten"] is False
    assert body["retrieval_query"] == "а в каких дозах?"
    assert spy.calls[-1][1]["retrieval_query"] is None

def test_followup_rewrite_prompt_has_no_pii(client, db_session, monkeypatch):
    """В LLM уходит только деперсонализированная история: ФИО/телефон/ДР
    маскируются scrub_for_llm до вызова модели."""
    from app.models import User
    from app.services import assistant_chat

    ch, (p1, _) = _setup(client, "th-followup-pii@test.ru")
    user = db_session.get(User, uuid.UUID(p1))
    user.fio = "Иванов Иван Иванович"  # roster-пациент → deny-list деперсонализации
    db_session.commit()

    tid = client.post(
        "/api/v1/assistant/threads", json={"patient_ids": [p1]}, headers=ch
    ).json()["id"]
    url = f"/api/v1/assistant/threads/{tid}/messages"
    llm = _fake_llm(monkeypatch, "Какие дозы препарата Авиандр?")
    monkeypatch.setattr(assistant_chat, "shared_factory", lambda: _SpyAgent())

    client.post(
        url,
        json={
            "content": (
                "Пациент Иванов Иван Иванович, тел. +7 999 123-45-67, "
                "д.р. 12.03.1954 — что известно про Авиандр?"
            )
        },
        headers=ch,
    )
    assert client.post(url, json={"content": "а в каких дозах?"}, headers=ch).status_code == 200

    assert len(llm) == 1
    sent = str(llm)
    for secret in ("Иванов", "123-45-67", "12.03.1954"):
        assert secret not in sent, f"ПДн утекли в LLM: {secret}"
    prompt = llm[0]["messages"][1]["content"]
    assert "[ФИО]" in prompt and "[ТЕЛЕФОН]" in prompt and "[ДАТА РОЖДЕНИЯ]" in prompt

def test_followup_redline_uses_only_current_question(client, kb_collection, monkeypatch):
    """Red line — по текущему вопросу: redline-реплика в истории не глушит
    поиск, а redline-вопрос отказывается даже при «безобидной» переформулировке."""
    ch, url = _new_thread(client, "th-followup-redline@test.ru")

    # История содержит лечение; переформулировка тоже «назначает» — но текущий
    # вопрос про шкалу, значит поиск должен состояться (redline_query = оригинал).
    _fake_llm(monkeypatch, "Назначь препарат от тревоги, какой порог PHQ-9?")
    assert client.post(url, json={"content": "Назначь препарат от тревоги"}, headers=ch).status_code == 200
    body = client.post(url, json={"content": "Какой порог тяжести у шкалы PHQ-9?"}, headers=ch).json()
    assert body["refused"] is False
    assert body["grounded"] is True
    assert body["citations"]
    # Обратный случай: текущий вопрос — redline. Переформулировки нет вовсе
    # (redline-текст в LLM не уходит), отказ — по текущему вопросу.
    llm = _fake_llm(monkeypatch, "Какой порог тяжести у шкалы PHQ-9?")
    body = client.post(url, json={"content": "Поставь диагноз депрессии"}, headers=ch).json()
    assert body["refused"] is True
    assert body["refusal_code"] == "DIAGNOSIS_REDLINE"
    assert body["citations"] == []
    assert body["retrieval_query"] == "Поставь диагноз депрессии"
    assert body["rewritten"] is False
    assert llm == []

def test_followup_question_retrieves_drug_chunk(client, kb_collection, monkeypatch):
    """Приёмка: «а в каких дозах?» после вопроса про препарат достаёт чанк
    ЛЕКАРСТВА (дозирование), а не NO_KB_DATA."""
    ch, _cid = clinician_ctx(client, "th-followup-kb@test.ru")
    tid = client.post("/api/v1/assistant/threads", json={"patient_ids": []}, headers=ch).json()["id"]
    url = f"/api/v1/assistant/threads/{tid}/messages"

    _fake_llm(monkeypatch, "Препарат Авиандр режим дозирования инструкция")
    assert client.post(
        url, json={"content": "Что известно про препарат Авиандр при депрессии?"}, headers=ch
    ).status_code == 200
    body = client.post(url, json={"content": "а в каких дозах?"}, headers=ch).json()

    assert body["rewritten"] is True
    assert body["refused"] is False
    assert body["grounded"] is True, body
    gids = {c["guideline_id"] for c in body["citations"]}
    assert "drug-aviandr" in gids, gids
    assert next(c for c in body["citations"] if c["guideline_id"] == "drug-aviandr")[
        "topic"
    ] == "ЛЕКАРСТВА"

class _SlowAgent:
    """Агент, не укладывающийся в дедлайн: поток спит дольше `assistant_deadline_s`."""

    def invoke(self, query, **kwargs):
        time.sleep(0.5)
        return {"answer": "поздний ответ", "citations": [], "topic": "ЛЕКАРСТВА"}


def test_thread_message_deadline_is_503_without_assistant_message(client, monkeypatch):
    """Дедлайн агента → 503 ASSISTANT_TIMEOUT; недописанный ответ в тред не идёт.

    Вопрос врача персистируется до вызова агента (аудит ПДн-доступа), ответа
    ассистента при таймауте быть не должно: иначе история треда показывала бы
    ответ, которого никто не получил.
    """
    from app.services import assistant_chat

    ch, url = _new_thread(client, "th-deadline@test.ru")
    monkeypatch.setattr(assistant_chat, "shared_factory", lambda: _SlowAgent())
    monkeypatch.setattr(assistant_chat.settings, "assistant_deadline_s", 0.05)

    resp = client.post(url, json={"content": "Что известно про Авиандр?"}, headers=ch)

    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["code"] == "ASSISTANT_TIMEOUT"
    assert body["status"] == 503 and body["type"] == "about:blank"
    assert body["instance"] == url
    assert resp.headers["retry-after"] == "5"
    assert "Traceback" not in resp.text and "поздний ответ" not in resp.text
    thread = client.get(url.rsplit("/messages", 1)[0], headers=ch).json()
    assert [m["role"] for m in thread["messages"]] == ["CLINICIAN"]


def test_agent_offload_uses_dedicated_limiter(monkeypatch):
    """Агент идёт в поток под своим лимитером, а не под общим пулом AnyIO (40)."""
    import anyio

    from app.services import assistant_chat

    assert assistant_chat.AGENT_LIMITER.total_tokens == settings.assistant_thread_limit
    assert settings.assistant_thread_limit == 8, "дефолт CC_ASSISTANT_THREAD_LIMIT"
    seen: dict[str, object] = {}

    class _Agent:
        def invoke(self, query, **kwargs):
            return {"answer": "ответ", "citations": []}

    async def spy(fn, **kwargs):
        seen["limiter"] = kwargs.get("limiter")
        return fn()

    monkeypatch.setattr(assistant_chat, "shared_factory", lambda: _Agent())
    monkeypatch.setattr(assistant_chat.anyio.to_thread, "run_sync", spy)

    assert anyio.run(assistant_chat._invoke_agent, "вопрос")["answer"] == "ответ"
    assert seen["limiter"] is assistant_chat.AGENT_LIMITER

def test_clinician_own_fio_masked_without_ner(client, db_session, monkeypatch):
    """ФИО самого врача в запросе маскируется deny-list'ом, а не только NER.

    Регресс ревью PR #40: `answer_query` брал только ростер, поэтому имя врача
    держалось исключительно на ML-детекции (при `CC_DEPERSONALIZE_NER=off` —
    никак).
    """
    import uuid

    from app.models import User
    from app.services import assistant_chat

    ch, cid = clinician_ctx(client, "th-clin-own@test.ru")
    db_session.get(User, uuid.UUID(cid)).fio = "Смирнова Ольга Петровна"
    db_session.commit()

    llm = _fake_llm(monkeypatch, "Какой порог тяжёлой депрессии по PHQ-9?")
    monkeypatch.setattr(assistant_chat, "shared_factory", lambda: _SpyAgent())

    resp = client.post(
        "/api/v1/assistant/ask",
        json={"query": "Смирнова Ольга Петровна спрашивает: какой порог PHQ-9?"},
        headers=ch,
    )
    assert resp.status_code == 200, resp.text
    sent = str(llm)
    assert "Смирнова" not in sent and "Петровна" not in sent



def test_thread_message_passes_history_to_agent(client, monkeypatch):
    """FR-17: история треда уходит в агент — контекст для LLM-ответа."""
    from app.services import assistant_chat

    ch, url = _new_thread(client, "th-history@test.ru")
    spy = _SpyAgent()
    monkeypatch.setattr(assistant_chat, "shared_factory", lambda: spy)

    client.post(url, json={"content": "Какой порог тяжести у шкалы PHQ-9?"}, headers=ch)
    assert spy.calls[-1][1]["history"] == [], "первый вопрос без истории"

    client.post(url, json={"content": "А у GAD-7?"}, headers=ch)
    history = spy.calls[-1][1]["history"]
    assert [m["role"] for m in history] == ["CLINICIAN", "ASSISTANT"]
    assert history[0]["content"] == "Какой порог тяжести у шкалы PHQ-9?"


def test_thread_answer_composed_by_llm_from_snippets(client, monkeypatch, kb_collection):
    """LLM пишет ответ по найденным фрагментам; цитаты — из настоящих чанков."""
    reply = "По PHQ-9 умеренная депрессия начинается с 10 баллов [1]."
    llm = _fake_llm(monkeypatch, reply)
    ch, _ = _setup(client, "th-llm-answer@test.ru")
    tid = client.post("/api/v1/assistant/threads", json={}, headers=ch).json()["id"]
    url = f"/api/v1/assistant/threads/{tid}/messages"

    body = client.post(
        url, json={"content": "Какой порог тяжести у шкалы PHQ-9?"}, headers=ch
    ).json()

    assert body["answer"] == reply
    assert body["grounded"] is True and body["refused"] is False
    cited = [c["guideline_id"] for c in body["citations"]]
    assert cited, "цитаты строятся из найденных сниппетов"
    assert cited == [cited[0]], "ссылка [1] → ровно один фрагмент"
    assert all(c["topic"] != "ПАЦИЕНТ" for c in body["citations"]), "тред без пациентов"
    answer_prompts = [
        b["messages"][-1]["content"]
        for b in llm
        if "Сниппеты базы знаний" in b["messages"][-1]["content"]
    ]
    assert answer_prompts, "ответ генерировался LLM, а не детерминированно"
    assert "Вопрос врача: Какой порог тяжести у шкалы PHQ-9?" in answer_prompts[0]
    assert "[1]" in answer_prompts[0]
