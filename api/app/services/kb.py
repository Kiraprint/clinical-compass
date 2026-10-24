"""Гибридный поиск по базе знаний (Qdrant). docs/07, docs/08.

Индексные точки: dense + sparse (NamedVector). Стратегия выдачи зависит от
эмбеддера (Embedder.use_sparse): hash-профиль — dense+sparse через Query API
prefetch + RRF (Fusion.RRF), нейросетевой bge-m3 — dense-only (см. hybrid_search).
Векторы считаются через SPI embedder (offline по умолчанию).
Коллекция `kb` хранит только открытый не-ПДн контент (docs/07 §3.5).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Sequence

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from app.config import settings
from app.services.embeddings import Embedder

log = logging.getLogger(__name__)

COLLECTION = settings.qdrant_collection  # docs/08 §3; в тестах — kb_test
DENSE_NAME = "dense"
SPARSE_NAME = "sparse"


class IndexConfigError(RuntimeError):
    """Индекс Qdrant несовместим с текущим эмбеддером — нужна переиндексация.

    Старт приложения падает с этим сообщением сразу (docs/08 §4), а не отдаёт
    500 на первом же запросе к ассистенту.
    """


def _dense_mismatch(actual: int, expected: int) -> IndexConfigError:
    return IndexConfigError(
        f"Коллекция '{COLLECTION}' построена с dense={actual}, а эмбеддер даёт "
        f"{expected}. Dense-вектора разных моделей несравнимы: пересоздайте индекс "
        f"`python -m app.kb_seed --reset` с текущим CC_EMBEDDER (это переиндексация "
        f"всего корпуса; после неё верните опубликованные гайдлайны и статьи "
        f"`python -m app.enrichment reindex`) или верните прежний CC_EMBEDDER."
    )


def ensure_collection(client: QdrantClient, dense_size: int) -> None:
    if client.collection_exists(COLLECTION):
        # Защита от несовпадения размерности при смене эмбеддера (CC_EMBEDDER):
        # hash=512, bge-m3=1024. Иначе upsert тихо запишет вектора неверной длины.
        info = client.get_collection(COLLECTION)
        actual = info.config.params.vectors[DENSE_NAME].size
        if actual != dense_size:
            raise _dense_mismatch(actual, dense_size)
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


def check_embedder_compatibility(client: QdrantClient, dense_size: int) -> None:
    """Стартовая проверка: коллекция построена тем же эмбеддером (docs/08 §4).

    Коллекции ещё нет — это не ошибка конфигурации (KB пуст до `kb_seed`):
    пишем предупреждение. Несовпадение размерности dense — IndexConfigError.
    """
    if not client.collection_exists(COLLECTION):
        log.warning(
            "Коллекция '%s' не найдена: KB-поиск пуст до `python -m app.kb_seed`",
            COLLECTION,
        )
        return
    actual = client.get_collection(COLLECTION).config.params.vectors[DENSE_NAME].size
    if actual != dense_size:
        raise _dense_mismatch(actual, dense_size)
    log.info("KB-индекс '%s' совместим: dense=%d", COLLECTION, dense_size)


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
    Sparse-вектор пишется всегда: профиль эмбеддера может смениться, а коллекция
    под него всё равно пересобирается.
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
    exclude_ids: set[str] | None = None,
    candidates: int | None = None,
    reranker=None,
    rrf_k: int | None = None,
) -> list[dict]:
    """Поиск по базе знаний: стратегия зависит от эмбеддера (Embedder.use_sparse).

    - hash (демо/тесты): dense + лексический sparse prefetch → RRF fusion.
      Hash-dense шумит, точный лексический канал вытаскивает термины;
    - dense-профили (bge-m3, qwen3-0.6b, e5-large-instruct, user-bge-m3):
      dense-only — RRF с лексическим каналом на RU→EN перефразах только мешает
      (bench/REPORT-retrieval.md).

    candidates — глубина пула ДО реранка/отсечки (CC_RETRIEVAL_CANDIDATES;
    None/0 → историческое max(limit*3, 10)). reranker — объект с методом
    rerank(query, hits, limit, exclude_ids=...) (app.services.reranker);
    без него — порядок каналов как есть. rrf_k — константа RRF (CC_RRF_K).

    Возвращает [{guideline_id, source_title, topic, score, text}]; при реранке
    score — скор кросс-энкодера (он же основа порога kb_min_score).
    """
    if not query.strip():
        return []
    fetch = candidates if candidates and candidates > 0 else max(limit * 3, 10)
    fetch = max(fetch, limit)
    dense = getattr(embedder, "query_dense", embedder.dense)(query)
    must: list = []
    must_not: list = []
    if topic:
        must.append(qm.FieldCondition(key="topic", match=qm.MatchValue(value=topic)))
    if exclude_ids:
        # Персональные исключения врача: источники остаются в КБ, но не в выдаче.
        must_not.append(qm.FieldCondition(key="guideline_id", match=qm.MatchAny(any=sorted(exclude_ids))))
    filter_ = qm.Filter(must=must, must_not=must_not) if (must or must_not) else None
    if getattr(embedder, "use_sparse", True):
        indices, values = embedder.sparse(query)
        prefetch = [
            qm.Prefetch(query=dense, using=DENSE_NAME, limit=fetch),
            qm.Prefetch(
                query=qm.SparseVector(indices=indices, values=values),
                using=SPARSE_NAME,
                limit=fetch,
            ),
        ]
        res = client.query_points(
            collection_name=COLLECTION,
            prefetch=prefetch,
            query=qm.RrfQuery(rrf=qm.Rrf(k=rrf_k or settings.rrf_k)),
            query_filter=filter_,
            limit=fetch,
            with_payload=True,
        )
    else:
        res = client.query_points(
            collection_name=COLLECTION,
            query=dense,
            using=DENSE_NAME,
            query_filter=filter_,
            limit=fetch,
            with_payload=True,
        )
    hits = [
        {
            "guideline_id": p.payload["guideline_id"],
            "source_title": p.payload["source_title"],
            "topic": p.payload.get("topic", ""),
            "text": p.payload.get("text", ""),
            "score": p.score,
        }
        for p in (res.points or [])
    ]
    if reranker is not None:
        ranked = reranker.rerank(query, hits, limit, exclude_ids=exclude_ids)
        if exclude_ids:
            # Реранк не имеет права воскресить исключённый врачом источник,
            # даже если его реализация забыла про фильтр (defense in depth).
            ranked = [h for h in ranked if h.get("guideline_id") not in exclude_ids]
        return ranked[:limit]
    return hits[:limit]


def count_points(client: QdrantClient) -> int:
    res = client.count(collection_name=COLLECTION, exact=True)
    return res.count


__all__ = [
    "COLLECTION",
    "IndexConfigError",
    "check_embedder_compatibility",
    "ensure_collection",
    "upsert_documents",
    "hybrid_search",
    "count_points",
]
