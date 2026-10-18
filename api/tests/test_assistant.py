# Контракт: docs/04 §3.1 (assistant), docs/07, docs/08; тесты — docs/06 §3.
"""ИИ-агент: гибридный поиск по базе знаний. Требует Qdrant (иначе skip)."""
from __future__ import annotations

import os

import pytest

from app.config import settings
from app.kb_seed import build_corpus

from .conftest import admin_ctx, auth_header, clinician_ctx, finish_screening, login, patient_ctx, register

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


def test_assistant_requires_clinician(client):
    h, _ = patient_ctx(client, "p-assist@test.ru")
    resp = client.post("/api/v1/assistant/ask", json={"query": "порог PHQ-9"}, headers=h)
    assert resp.status_code == 403
    assert resp.json()["code"] == "ROLE_MISMATCH"


def test_kb_corpus_includes_drugs_and_guidelines():
    """Корпус KB: шкалы + триаж + красные линии + препараты (Авиандр) + КР Минздрава."""
    docs = build_corpus()
    gids = {d["guideline_id"] for d in docs}
    topics = {d["topic"] for d in docs}
    assert "drug-aviandr" in gids and "drug-aviandr-safety" in gids
    assert "kr-gtr-f41-1" in gids
    assert "ЛЕКАРСТВА" in topics
    # Чанк Авиандра цитирует регистрационное удостоверение (усл.7 кейса — ГРЛС).
    aviandr = next(d for d in docs if d["guideline_id"] == "drug-aviandr")
    assert "РУ" in aviandr["text"] and "ЛП-" in aviandr["text"]


def test_assistant_grounded_answer_with_citations(client, kb_collection):
    h, _ = clinician_ctx(client, "c-assist@test.ru")
    resp = client.post("/api/v1/assistant/ask", json={"query": "Какой порог тяжести у шкалы PHQ-9?"}, headers=h)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["grounded"] is True and body["refused"] is False
    assert body["citations"], "ответ должен содержать цитаты источников"
    assert any("PHQ-9" in c["source_title"] for c in body["citations"])
    assert "Источники:" in body["answer"]


def test_assistant_refuses_diagnosis(client, kb_collection):
    h, _ = clinician_ctx(client, "c-assist2@test.ru")
    resp = client.post("/api/v1/assistant/ask", json={"query": "Поставь диагноз депрессии"}, headers=h)
    body = resp.json()
    assert body["refused"] is True
    assert body["refusal_code"] == "DIAGNOSIS_REDLINE"
    assert body["grounded"] is False


def test_assistant_refuses_treatment(client, kb_collection):
    h, _ = clinician_ctx(client, "c-assist3@test.ru")
    resp = client.post("/api/v1/assistant/ask", json={"query": "Назначь препарат от тревоги"}, headers=h)
    body = resp.json()
    assert body["refused"] is True and body["refusal_code"] == "TREATMENT_REDLINE"


def test_assistant_offtopic_returns_no_kb_data(client, kb_collection):
    h, _ = clinician_ctx(client, "c-assist4@test.ru")
    resp = client.post("/api/v1/assistant/ask", json={"query": "Какая погода в Москве сегодня?"}, headers=h)
    body = resp.json()
    assert body["grounded"] is False
    assert body["refusal_code"] == "NO_KB_DATA"


def test_assistant_topic_filter(client, kb_collection):
    h, _ = clinician_ctx(client, "c-assist5@test.ru")
    resp = client.post(
        "/api/v1/assistant/ask",
        json={"query": "триаж RED", "topic": "ТРИАЖ"},
        headers=h,
    )
    body = resp.json()
    assert body["grounded"] is True
    assert all(c["topic"] == "ТРИАЖ" for c in body["citations"])


def test_assistant_status_reports_points(client, kb_collection):
    h, _ = clinician_ctx(client, "c-assist6@test.ru")
    resp = client.get("/api/v1/assistant/status", headers=h)
    assert resp.status_code == 200
    assert resp.json()["points"] >= 8


def test_assistant_ask_is_audited(client, kb_collection):
    h, uid = clinician_ctx(client, "c-assist7@test.ru")
    client.post("/api/v1/assistant/ask", json={"query": "порог PHQ-9"}, headers=h)
    ah, _ = admin_ctx(client, "a-assist@test.ru")
    audit = client.get("/api/v1/admin/audit", headers=ah).json()
    items = audit["items"]
    assert any(i["action"] == "ASK_ASSISTANT" for i in items)

def test_ensure_collection_guards_dense_size_mismatch(client, kb_collection, monkeypatch):
    """Смена эмбеддера (CC_EMBEDDER hash=512 → bge-m3=1024) не должна тихо
    записать вектора неверной длины в существующую коллекцию (docs/08 §4)."""
    from qdrant_client import QdrantClient

    from app.services import kb

    c = QdrantClient(url=settings.qdrant_url)
    probe = "kb_test_guard"
    if c.collection_exists(probe):
        c.delete_collection(probe)
    from qdrant_client.http import models as qm

    c.create_collection(probe, vectors_config={kb.DENSE_NAME: qm.VectorParams(size=512, distance="Cosine")})
    monkeypatch.setattr(kb, "COLLECTION", probe)
    try:
        with pytest.raises(RuntimeError):
            kb.ensure_collection(c, 1024)
    finally:
        monkeypatch.undo()
        c.delete_collection(probe)


# --- @-упоминание пациента: контекст в агент (docs/07 §7) ---

GREEN_ANSWERS = {f"q{i}": 0 for i in range(1, 10)}  # PHQ-9 all-0 → GREEN


def _grant(client, admin_h, patient_id, clinician_id) -> None:
    resp = client.post(
        "/api/v1/admin/roster",
        json={"patient_id": patient_id, "clinician_id": clinician_id},
        headers=admin_h,
    )
    assert resp.status_code == 201, resp.text


def test_assistant_with_patient_context_grounds_on_card(client, kb_collection):
    """@-упоминание: контекст пациента подмешивается, цитируется карта (topic=ПАЦИЕНТ)."""
    ph, pid = patient_ctx(client, "pap@test.ru")
    ch, cid = clinician_ctx(client, "cap@test.ru")
    ah, _ = admin_ctx(client, "aap@test.ru")
    _grant(client, ah, pid, cid)
    finish_screening(client, ph, GREEN_ANSWERS)  # assessment для карты пациента

    resp = client.post(
        "/api/v1/assistant/ask",
        json={"query": "как динамика у пациента", "patient_id": pid},
        headers=ch,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["grounded"] is True
    assert any(c["topic"] == "ПАЦИЕНТ" for c in body["citations"]), body["citations"]


def test_assistant_patient_context_roster_gated(client, kb_collection):
    """Пациент не в ротстере врача → 403 NO_ROSTER."""
    ph, pid = patient_ctx(client, "pap2@test.ru")
    ch, _ = clinician_ctx(client, "cap2@test.ru")
    resp = client.post(
        "/api/v1/assistant/ask",
        json={"query": "динамика", "patient_id": pid},
        headers=ch,
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "NO_ROSTER"


def test_assistant_patient_context_consent_gated(client, kb_collection):
    """Пациент в ротстере, но без активного согласия → 403 CONSENT_REQUIRED (152-ФЗ)."""
    reg = register(client, "papc@test.ru")
    ph = auth_header(login(client, "papc@test.ru")["access_token"])  # без consent
    ch, cid = clinician_ctx(client, "capc@test.ru")
    ah, _ = admin_ctx(client, "aapc@test.ru")
    _grant(client, ah, reg["id"], cid)
    resp = client.post(
        "/api/v1/assistant/ask",
        json={"query": "динамика", "patient_id": reg["id"]},
        headers=ch,
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "CONSENT_REQUIRED"


def test_assistant_patient_context_audited(client, kb_collection):
    """Загрузка контекста пациента аудируется ASK_ASSISTANT_WITH_PATIENT (спецкатегория)."""
    ph, pid = patient_ctx(client, "pap3@test.ru")
    ch, cid = clinician_ctx(client, "cap3@test.ru")
    ah, _ = admin_ctx(client, "aap3@test.ru")
    _grant(client, ah, pid, cid)
    finish_screening(client, ph, GREEN_ANSWERS)

    client.post(
        "/api/v1/assistant/ask",
        json={"query": "контекст пациента", "patient_id": pid},
        headers=ch,
    )
    resp = client.get("/api/v1/admin/audit", headers=ah)
    assert resp.status_code == 200
    actions = [e["action"] for e in resp.json()["items"]]
    assert "ASK_ASSISTANT_WITH_PATIENT" in actions
