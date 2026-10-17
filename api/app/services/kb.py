"""Гибридный поиск по базе знаний (Qdrant). docs/07, docs/08.

Индексные точки: dense + sparse (NamedVector), слияние через Query API prefetch
+ RRF (Fusion.RRF). Векторы считаются через SPI embedder (offline по умолчанию).
Коллекция `kb` хранит только открытый не-ПДн контент (docs/07 §3.5).
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Sequence

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from app.config import settings
from app.services.embeddings import Embedder

COLLECTION = settings.qdrant_collection  # docs/08 §3; в тестах — kb_test
DENSE_NAME = "dense"
SPARSE_NAME = "sparse"


def ensure_collection(client: QdrantClient, dense_size: int) -> None:
    if client.collection_exists(COLLECTION):
        # Защита от несовпадения размерности при смене эмбеддера (CC_EMBEDDER):
        # hash=512, bge-m3=1024. Иначе upsert тихо запишет вектора неверной длины.
        info = client.get_collection(COLLECTION)
        actual = info.config.params.vectors[DENSE_NAME].size
        if actual != dense_size:
            raise RuntimeError(
                f"Коллекция '{COLLECTION}' создана с dense={actual}, а эмбеддер даёт "
                f"{dense_size}. Пересоздайте коллекцию (kb_seed --reset) или оставьте "
                f"прежний CC_EMBEDDER."
            )
        return
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config={
            DENSE_NAME: qm.VectorParams(size=dense_size, distance=qm.Distance.COSINE),
        },
        sparse_vectors_config={
            SPARSE_NAME: qm.SparseVectorParams(),
        },
    )


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _point_id(text: str) -> str:
    """Детерминированный UUID из text_hash — идемпотентная переиндексация."""
    return str(uuid.UUID(hashlib.md5(text.encode("utf-8")).hexdigest()))


def upsert_documents(
    client: QdrantClient,
    embedder: Embedder,
    documents: Sequence[dict],
) -> int:
    """Загружает чанки. Каждый doc: {guideline_id, source_title, topic, text}.

    Дедупликация по text_hash; повторный заход не плодит дубли (docs/08 §5).
    """
    ensure_collection(client, embedder.dense_size)
    points: list[qm.PointStruct] = []
    for i, doc in enumerate(documents):
        text = doc["text"].strip()
        text_hash = _hash(text)
        dense = embedder.dense(text)
        indices, values = embedder.sparse(text)
        points.append(
            qm.PointStruct(
                id=_point_id(text),
                vector={
                    DENSE_NAME: dense,
                    SPARSE_NAME: qm.SparseVector(indices=indices, values=values),
                },
                payload={
                    "guideline_id": doc["guideline_id"],
                    "source_title": doc["source_title"],
                    "topic": doc.get("topic", ""),
                    "chunk_index": i,
                    "text": text,
                    "text_hash": text_hash,
                },
            )
        )
    client.upsert(collection_name=COLLECTION, points=points, wait=True)
    return len(points)


def hybrid_search(
    client: QdrantClient,
    embedder: Embedder,
    query: str,
    *,
    topic: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """Гибридный поиск: dense + sparse prefetch → RRF fusion.

    Возвращает [{guideline_id, source_title, topic, score, text}] — текст берём
    из базового фрагмента (полный корпус хранится отдельно; здесь метаданные).
    """
    dense = embedder.dense(query)
    indices, values = embedder.sparse(query)
    prefetch = [
        qm.Prefetch(query=dense, using=DENSE_NAME, limit=max(limit * 3, 10)),
        qm.Prefetch(query=qm.SparseVector(indices=indices, values=values), using=SPARSE_NAME, limit=max(limit * 3, 10)),
    ]
    filter_ = qm.Filter(must=[qm.FieldCondition(key="topic", match=qm.MatchValue(value=topic))]) if topic else None
    res = client.query_points(
        collection_name=COLLECTION,
        prefetch=prefetch,
        query=qm.FusionQuery(fusion=qm.Fusion.RRF),
        query_filter=filter_,
        limit=limit,
        with_payload=True,
    )
    return [
        {
            "guideline_id": p.payload["guideline_id"],
            "source_title": p.payload["source_title"],
            "topic": p.payload.get("topic", ""),
            "text": p.payload.get("text", ""),
            "score": p.score,
        }
        for p in (res.points or [])
    ]


def count_points(client: QdrantClient) -> int:
    res = client.count(collection_name=COLLECTION, exact=True)
    return res.count


__all__ = [
    "COLLECTION",
    "ensure_collection",
    "upsert_documents",
    "hybrid_search",
    "count_points",
]
