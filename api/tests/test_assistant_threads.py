# FR-21: сессии клинического ассистента — persistent-контекст и мультипациент.
# Контракт: docs/04 §3.1, docs/05 E9; тесты — docs/06 §3.
"""Треды ассистента: изоляция врачей, roster-гейт, история, аудит, каскад."""
from __future__ import annotations

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

def test_history_window_trims_old_messages(client, db_session, kb_collection):
    """Контекст треда: ≤10 последних сообщений и ≤6000 символов, новые важнее."""
    from app.models import AssistantMessage
    from app.services import assistant_chat

    ch, _ = _setup(client, "th-hist@test.ru")
    tid = client.post("/api/v1/assistant/threads", json={}, headers=ch).json()["id"]
    thread_uuid = uuid.UUID(tid)
    for i in range(15):
        db_session.add(
            AssistantMessage(
                thread_id=thread_uuid,
                role="CLINICIAN" if i % 2 == 0 else "ASSISTANT",
                content=f"сообщение-{i:02d} " + "x" * 100,
                citations=[],
                created_at=_TS + timedelta(seconds=i),
            )
        )
    db_session.commit()

    history = assistant_chat._history(db_session, thread_uuid)
    assert len(history) == 10, "в контекст идут только 10 последних сообщений"
    assert history[-1].content.startswith("сообщение-14")
    assert history[0].content.startswith("сообщение-05")

    # Бюджет символов: длинные сообщения (по ~700) режутся до 6000 суммарно.
    db_session.query(AssistantMessage).filter(
        AssistantMessage.thread_id == thread_uuid
    ).delete()
    db_session.commit()

    for i in range(12):
        db_session.add(
            AssistantMessage(
                thread_id=thread_uuid,
                role="CLINICIAN",
                content=f"длинное-{i:02d} " + "y" * 700,
                citations=[],
                created_at=_TS + timedelta(seconds=i),
            )
        )
    db_session.commit()
    history = assistant_chat._history(db_session, thread_uuid)
    assert sum(len(m.content) for m in history) <= assistant_chat.HISTORY_CHARS
    assert len(history) < 12
    assert history[-1].content.startswith("длинное-11")

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

def test_history_is_scrubbed_before_retrieval():
    """ФИО из прошлых реплик маскируются в retrieval-строке (как текущий вопрос)."""
    from app.models import AssistantMessage
    from app.services import assistant_chat

    msgs = [
        AssistantMessage(role="CLINICIAN", content="Иванов Иван Иванович, 44 года"),
        AssistantMessage(role="ASSISTANT", content="Уточните, пожалуйста"),
    ]
    block = assistant_chat._history_block(msgs, ["Иванов Иван Иванович"])
    assert "Иванов" not in block
    assert "[ФИО]" in block
