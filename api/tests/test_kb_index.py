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


def _fake_dense_client(scores: list[float]):
    """Фейк Qdrant: пул из n точек с заданными косинусами (dense-профиль)."""

    class _Point:
        def __init__(self, i: int, score: float) -> None:
            self.payload = {
                "guideline_id": f"g-{i}",
                "source_title": f"Источник {i}",
                "topic": "ШКАЛЫ",
                "text": f"чанк {i}",
            }
            self.score = score

    class _Client:
        def query_points(self, **kwargs):
            class _Res:
                pass

            res = _Res()
            res.points = [_Point(i, s) for i, s in enumerate(scores)]
            return res

    return _Client()


class _DenseOnly(HashEmbedder):
    use_sparse = False


def test_min_score_gates_on_full_pool():
    """Порог «нет данных» — по лучшему скору пула; хвост выдачи не режется."""
    emb = _DenseOnly()
    assert [h["guideline_id"] for h in kb.hybrid_search(_fake_dense_client([0.71, 0.55]), emb, "q", limit=2, min_score=0.45)] == ["g-0", "g-1"]
    # Хвост ниже порога остаётся: порог — только про «в KB ничего нет».
    assert len(kb.hybrid_search(_fake_dense_client([0.71, 0.44]), emb, "q", limit=2, min_score=0.45)) == 2
    assert kb.hybrid_search(_fake_dense_client([0.37]), emb, "q", limit=2, min_score=0.45) == []
    assert kb.hybrid_search(_fake_dense_client([]), emb, "q", limit=2, min_score=0.45) == []


def test_reseed_same_source_does_not_duplicate_chunks(monkeypatch):
    """Повторный засев КР заменяет чанки прошлой нарезки, статьи не трогает.

    Point id = md5(text): при смене CC_KB_CHUNK_* чанки прошлой генерации
    остались бы рядом с новыми. upsert_corpus сносит точки источника по
    source_title + префиксу guideline_id `kr-` перед записью — count тот же,
    --reset не нужен (P2 ревью).
    """
    from app import kb_seed

    probe = "kb_test_reseed"
    client = _fresh(probe)
    monkeypatch.setattr(kb, "COLLECTION", probe)
    base = {"source_title": "КР X", "topic": "РЕКОМЕНДАЦИИ"}
    gen1 = (
        {**base, "guideline_id": "kr-x", "text": "A" * 300},
        {**base, "guideline_id": "kr-x-t0", "text": "B" * 300},
    )
    gen2 = (
        {**base, "guideline_id": "kr-x", "text": "A" * 300},
        {**base, "guideline_id": "kr-x-t0", "text": "C" * 200},
    )
    # Тот же source_title, но не КР: префиксный фильтр обязан его пощадить.
    article = (
        {"guideline_id": "who-1", "source_title": "КР X", "topic": "СТАТЬИ",
         "text": "статья обзора с совпавшим заголовком"},
    )
    try:
        kb_seed.upsert_corpus(client, HashEmbedder(), gen1 + article)
        assert kb.count_points(client) == 3
        assert kb_seed.upsert_corpus(client, HashEmbedder(), gen2) == 2
        assert kb.count_points(client) == 3, "повторный сид добавил чанк прошлой нарезки"
        hits = kb.hybrid_search(client, HashEmbedder(), "статья обзора", limit=5)
        assert any(h["guideline_id"] == "who-1" for h in hits), "статья удалена вместе с КР"
    finally:
        monkeypatch.undo()
        client.delete_collection(probe)

def test_seed_embeds_before_deleting(monkeypatch):
    """Сбой эмбеддера не сносит опубликованные чанки: эмбеддинги ДО удаления."""
    from app import kb_seed

    probe = "kb_test_seed_order"
    client = _fresh(probe)
    monkeypatch.setattr(kb, "COLLECTION", probe)
    doc = {"guideline_id": "kr-y", "source_title": "КР Y", "topic": "РЕКОМЕНДАЦИИ",
           "text": "A" * 300}

    class _Broken(HashEmbedder):
        def dense(self, text: str):  # noqa: ARG002 — эмулируем упавший сайдкар
            raise RuntimeError("сайдкар лёг")

    try:
        kb.upsert_documents(client, HashEmbedder(), [doc])
        assert kb.count_points(client) == 1
        with pytest.raises(RuntimeError):
            kb_seed.upsert_corpus(client, _Broken(), ({**doc, "text": "B" * 300},))
        assert kb.count_points(client) == 1, "старые чанки снесены до эмбеддингов"
    finally:
        monkeypatch.undo()
        client.delete_collection(probe)

def test_build_points_rejects_short_embedder_output():
    """Короткий ответ эмбеддера не должен молча терять точки (zip-обрезка)."""
    docs = [
        {"guideline_id": "kr-z", "source_title": "КР Z", "text": "раз"},
        {"guideline_id": "kr-z-t0", "source_title": "КР Z", "text": "два"},
    ]

    class _Short(HashEmbedder):
        def dense_batch(self, texts):  # noqa: ARG002 — намеренно короткий ответ
            return [[0.1] * self.dense_size]

    with pytest.raises(RuntimeError, match="dense-векторов"):
        kb.build_points(_Short(), docs)
