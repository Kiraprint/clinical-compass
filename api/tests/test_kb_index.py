"""Совместимость эмбеддера с индексом KB и стратегия выдачи (docs/08 §4).

Старт приложения обязан падать с инструкцией при несовпадении dense-размерности
коллекции (а не 500 на первом запросе), а нейросетевой профиль — искать
dense-only, не трогая sparse-канал.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services import kb
from app.services.embeddings import HashEmbedder


def _qdrant_available() -> bool:
    try:
        from qdrant_client import QdrantClient

        QdrantClient(url=settings.qdrant_url, timeout=2).get_collections()
        return True
    except Exception:  # noqa: BLE001 — Qdrant не поднят (локальный прогон без стека)
        return False


pytestmark = pytest.mark.skipif(not _qdrant_available(), reason="Qdrant недоступен (CC_QDRANT_URL)")


def _client():
    from qdrant_client import QdrantClient

    return QdrantClient(url=settings.qdrant_url, timeout=10)


def _fresh(collection: str):
    client = _client()
    if client.collection_exists(collection):
        client.delete_collection(collection)
    return client


def test_dense_size_mismatch_error_is_actionable(monkeypatch):
    from qdrant_client.http import models as qm

    probe = "kb_test_compat"
    client = _fresh(probe)
    client.create_collection(probe, vectors_config={kb.DENSE_NAME: qm.VectorParams(size=512, distance="Cosine")})
    monkeypatch.setattr(kb, "COLLECTION", probe)
    try:
        with pytest.raises(kb.IndexConfigError) as exc:
            kb.check_embedder_compatibility(client, 1024)
        message = str(exc.value)
        assert "dense=512" in message and "даёт 1024" in message
        assert "kb_seed --reset" in message
    finally:
        monkeypatch.undo()
        client.delete_collection(probe)


def test_missing_collection_is_not_a_config_error(monkeypatch):
    probe = "kb_test_absent"
    client = _fresh(probe)
    monkeypatch.setattr(kb, "COLLECTION", probe)
    try:
        kb.check_embedder_compatibility(client, 1024)  # только предупреждение
        assert not client.collection_exists(probe)
    finally:
        monkeypatch.undo()


def test_startup_validation_fails_on_mismatched_index(monkeypatch):
    from qdrant_client.http import models as qm

    from app import main
    from app.services import embeddings

    probe = "kb_test_startup"
    client = _fresh(probe)
    client.create_collection(probe, vectors_config={kb.DENSE_NAME: qm.VectorParams(size=512, distance="Cosine")})

    class _Dense1024:
        dense_size = 1024

    monkeypatch.setattr(kb, "COLLECTION", probe)
    monkeypatch.setattr(embeddings, "get_embedder", lambda: _Dense1024())
    try:
        with pytest.raises(kb.IndexConfigError):
            main._validate_kb_index()
    finally:
        monkeypatch.undo()
        client.delete_collection(probe)


def test_dense_only_embedder_never_calls_sparse(monkeypatch):
    """bge-m3-профиль: dense-only выдача (RRF с лексикой роняет RU-качество)."""
    probe = "kb_test_dense_only"
    client = _fresh(probe)
    monkeypatch.setattr(kb, "COLLECTION", probe)
    docs = [
        {
            "guideline_id": "g-phq",
            "source_title": "Шкала PHQ-9",
            "topic": "ШКАЛЫ",
            "text": "Шкала PHQ-9, порог тяжёлой депрессии 20 баллов, суицидальный пункт 9",
        },
        {
            "guideline_id": "g-gad",
            "source_title": "Шкала GAD-7",
            "topic": "ШКАЛЫ",
            "text": "Шкала GAD-7 измеряет генерализованное тревожное расстройство за две недели",
        },
    ]

    class DenseOnly(HashEmbedder):
        use_sparse = False

        def sparse(self, text: str):
            raise AssertionError("dense-only профиль не должен обращаться к sparse")

    try:
        kb.upsert_documents(client, HashEmbedder(), docs)
        hits = kb.hybrid_search(client, DenseOnly(), "порог тяжёлой депрессии PHQ-9", limit=2)
        assert hits, "dense-only поиск вернул пустую выдачу"
        assert hits[0]["guideline_id"] == "g-phq"
    finally:
        monkeypatch.undo()
        client.delete_collection(probe)


def test_dense_top_gate_keeps_crosslingual_and_rejects_offtopic():
    from app.services.agent import _dense_top_gate

    relevant = [{"score": 0.71}, {"score": 0.55}]
    assert _dense_top_gate(relevant, 0.45) == relevant
    # Хвост выдачи ниже порога не режем: порог — только про «в KB ничего нет».
    tail = [{"score": 0.71}, {"score": 0.44}]
    assert _dense_top_gate(tail, 0.45) == tail
    assert _dense_top_gate([{"score": 0.37}], 0.45) == []
    assert _dense_top_gate([], 0.45) == []
