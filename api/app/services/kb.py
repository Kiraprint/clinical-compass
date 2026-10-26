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
from qdrant_client.http.exceptions import UnexpectedResponse

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
        f"{expected}. Dense-вектора разных моделей несравнимы. Сначала задайте "
        f"ОТДЕЛЬНУЮ коллекцию под новый профиль (CC_QDRANT_COLLECTION=kb_<profile>"
        f" и `python -m app.kb_seed`) — рабочая `kb` и документированный откат "
        f"(docs/08 §4.7) остаются целы. `python -m app.kb_seed --reset` — только "
        f"как последнее средство: он сносит коллекцию '{COLLECTION}' целиком "
        f"(после него верните гайдлайны и статьи `python -m app.enrichment "
        f"reindex`). Либо верните прежний CC_EMBEDDER."
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


def build_points(embedder: Embedder, documents: Sequence[dict]) -> list[qm.PointStruct]:
    """Точки для upsert: эмбеддинги + payload, без записи в Qdrant.

    Отделено от записи, чтобы вызывающий мог СНАЧАЛА посчитать эмбеддинги и
    только потом сносить старые точки: сбой эмбеддера не должен оставить
    коллекцию без гайдлайнов (так же устроен enrichment/index.py).
    Каждый doc: {guideline_id, source_title, topic, text}.
    """
    texts = [doc["text"].strip() for doc in documents]
    # Пачкой, если эмбеддер умеет (RemoteEmbedder: пачка HTTP вместо POST на
    # чанк — ре-сид не делает ~3362 запроса).
    batch = getattr(embedder, "dense_batch", None)
    dense_vectors = batch(texts) if batch is not None else [embedder.dense(t) for t in texts]
    if len(dense_vectors) != len(texts):
        raise RuntimeError(
            f"эмбеддер вернул {len(dense_vectors)} dense-векторов на {len(texts)} чанков"
        )
    points: list[qm.PointStruct] = []
    for i, (doc, text, dense) in enumerate(zip(documents, texts, dense_vectors)):
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
                    "text_hash": _hash(text),
                },
            )
        )
    return points

def upsert_points(
    client: QdrantClient,
    points: Sequence[qm.PointStruct],
    *,
    dense_size: int,
) -> int:
    """Записать готовые точки (см. build_points). Возвращает их число."""
    ensure_collection(client, dense_size)
    if not points:
        return 0
    client.upsert(collection_name=COLLECTION, points=list(points), wait=True)
    return len(points)

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
    return upsert_points(
        client, build_points(embedder, documents), dense_size=embedder.dense_size
    )

_SCROLL_PAGE = 256
_DELETE_BATCH = 512

def delete_sources(
    client: QdrantClient,
    source_titles: Sequence[str],
    *,
    id_prefix: str | None = None,
    wait: bool = True,
) -> int:
    """Удалить точки указанных источников перед повторным засевом.

    Point id производен от текста чанка (`_point_id`), а границы нарезки зависят
    от CC_KB_CHUNK_*: повторный `kb_seed --with-guidelines` с другой нарезкой
    оставил бы чанки прошлой генерации рядом с новыми (id другие). Фильтр по
    source_title — стабильному имени источника — сносит все поколения сразу,
    поэтому count после повторного сида не растёт (docs/08 §5).

    `id_prefix` дополнительно сужает удаление до точек, у которых guideline_id
    начинается с префикса (`kr-` у КР): Qdrant не умеет prefix-фильтр по payload,
    поэтому точки читаются scroll'ом и фильтруются на клиенте. Возвращает число
    удалённых точек.
    """
    titles = sorted({t for t in source_titles if t})
    if not titles:
        return 0
    source_filter = qm.Filter(
        must=[qm.FieldCondition(key="source_title", match=qm.MatchAny(any=titles))]
    )
    ids: list[str] = []
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=COLLECTION,
            scroll_filter=source_filter,
            limit=_SCROLL_PAGE,
            offset=offset,
            with_payload=["guideline_id"],
            with_vectors=False,
        )
        for point in points:
            payload = point.payload or {}
            if id_prefix is not None and not str(payload.get("guideline_id", "")).startswith(id_prefix):
                continue
            ids.append(str(point.id))
        if offset is None:
            break
    for start in range(0, len(ids), _DELETE_BATCH):
        client.delete(
            collection_name=COLLECTION,
            points_selector=qm.PointIdsList(points=ids[start : start + _DELETE_BATCH]),
            wait=wait,
        )
    return len(ids)


def is_visible(hit: dict, work_ids: set[str] | None) -> bool:
    """FR-36: статья видна, только если врач одобрил её сам.

    `work_ids is None` — фильтр не применяется (служебные пути). Гайдлайны ВОЗ
    и seed-корпус (нет work_id) видны всем без решения врача.
    """
    if work_ids is None:
        return True
    if hit.get("kind") == "guideline" or not hit.get("work_id"):
        return True
    return str(hit["work_id"]) in work_ids


def _query_kb(client: QdrantClient, **kwargs):
    """Запрос к коллекции KB; её отсутствие — пустая база, а не сбой запроса.

    Сброс индекса (`kb_seed --reset`) и первый запуск до индексации оставляют
    Qdrant без коллекции: Qdrant отвечает 404, и без этой обработки каждый
    вопрос ассистента отдавал бы 500 вместо «в базе знаний нет данных»
    (docs/08 §4). Возвращает None, если коллекции нет.
    """
    try:
        return client.query_points(collection_name=COLLECTION, **kwargs)
    except UnexpectedResponse as exc:
        if getattr(exc, "status_code", None) == 404:
            log.warning(
                "Коллекция '%s' не найдена: KB-поиск пуст до `python -m app.kb_seed`",
                COLLECTION,
            )
            return None
        raise


def hybrid_search(
    client: QdrantClient,
    embedder: Embedder,
    query: str,
    *,
    topic: str | None = None,
    topics: Sequence[str] | None = None,
    limit: int = 5,
    exclude_ids: set[str] | None = None,
    work_ids: set[str] | None = None,
    candidates: int | None = None,
    reranker=None,
    rrf_k: int | None = None,
    min_score: float | None = None,
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
    без него — порядок каналов как есть. rrf_k — константа RRF (CC_RRF_K,
    не задана → серверный дефолт Qdrant). min_score — порог «нет релевантного
    ответа» по dense-косинусу профиля: применяется ко ВСЕМУ пулу до реранка и
    отсечки (иначе реранк мог выбросить лучший по первичному скору чанк из
    top-limit и включить отказ на релевантном запросе). Hash-профиль гейтится
    лексически в агенте — RRF-скоры с косинусом несравнимы.

    work_ids (FR-36) — allow-list статей, которые врач одобрил лично. None —
    фильтра нет (служебные пути), пустое множество — ни одной статьи. Видны
    всегда: точки без work_id (seed-корпус: шкалы, препараты, демо) и
    kind='guideline' (гайдлайны ВОЗ) — они не требуют решения врача.

    topic / topics — разделы корпуса (docs/08 §3). Один раздел фильтруется
    MatchValue, несколько — MatchAny: вопрос бывает одновременно про препарат и
    про доказательства, и тогда LLM-роутер (FR-14) возвращает две темы. Пусто —
    поиск по всем разделам.

    Возвращает [{guideline_id, source_title, topic, score, text, work_id,
    kind}]; при реранке score — скор кросс-энкодера (он же основа порога
    kb_min_score). work_id/kind — для персонального фильтра видимости в агенте.
    """
    if not query.strip():
        return []
    fetch = candidates if candidates and candidates > 0 else max(limit * 3, 10)
    fetch = max(fetch, limit)
    dense = getattr(embedder, "query_dense", embedder.dense)(query)
    must: list = []
    must_not: list = []
    should: list = []
    topic_list = tuple(topics or ()) or ((topic,) if topic else ())
    if len(topic_list) == 1:
        must.append(qm.FieldCondition(key="topic", match=qm.MatchValue(value=topic_list[0])))
    elif topic_list:
        must.append(qm.FieldCondition(key="topic", match=qm.MatchAny(any=list(topic_list))))
    if exclude_ids:
        # Персональные исключения врача: источники остаются в КБ, но не в выдаче.
        must_not.append(qm.FieldCondition(key="guideline_id", match=qm.MatchAny(any=sorted(exclude_ids))))
    if work_ids is not None:
        # FR-36: статья видна врачу, только если он одобрил её сам. should —
        # OR-группа поверх must: seed-точки без work_id (шкалы/препараты/демо) и
        # гайдлайны ВОЗ (kind='guideline') видны всем без решения врача.
        if work_ids:
            should.append(qm.FieldCondition(key="work_id", match=qm.MatchAny(any=sorted(work_ids))))
        should.append(
            qm.IsEmptyCondition(is_empty=qm.PayloadField(key="work_id"))
        )
        should.append(qm.FieldCondition(key="kind", match=qm.MatchValue(value="guideline")))
    filter_ = (
        qm.Filter(must=must, should=should, must_not=must_not)
        if (must or should or must_not)
        else None
    )
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
        # CC_RRF_K не задан → историческая серверная форма FusionQuery(RRF):
        # k выбирает Qdrant, выдача hash-профиля не меняется молча. Явный
        # retrieval-профиль (CC_RRF_K=60) → RrfQuery(Rrf(k=60)).
        k = rrf_k if rrf_k is not None else settings.rrf_k
        fusion = (
            qm.RrfQuery(rrf=qm.Rrf(k=k))
            if k and k > 0
            else qm.FusionQuery(fusion=qm.Fusion.RRF)
        )
        res = _query_kb(
            client,
            prefetch=prefetch,
            query=fusion,
            query_filter=filter_,
            limit=fetch,
            with_payload=True,
        )
    else:
        res = _query_kb(
            client,
            query=dense,
            using=DENSE_NAME,
            query_filter=filter_,
            limit=fetch,
            with_payload=True,
        )
    if res is None:  # коллекции нет — пустая выдача (WARNING уже залогирован)
        return []
    hits = [
        {
            # id точки — стабильный идентификатор чанка для ссылок в ответах
            # (черновик протокола, docs/18 §5.4: sources[].id). У настоящей точки
            # Qdrant он есть всегда, у фейковых точек в тестах — нет.
            "id": str(getattr(p, "id", "") or ""),
            "guideline_id": p.payload["guideline_id"],
            "source_title": p.payload["source_title"],
            "topic": p.payload.get("topic", ""),
            "text": p.payload.get("text", ""),
            # Провенанс для sources[].url: seed-корпус ссылки не несёт (""),
            # гайдлайны/статьи — несут (enrichment/index.py).
            "url": p.payload.get("source_url") or "",
            "score": p.score,
            # FR-36: work_id/kind — для персонального фильтра видимости в агенте.
            "work_id": p.payload.get("work_id"),
            "kind": p.payload.get("kind"),
        }
        for p in (res.points or [])
    ]
    # Порог — про лучший скор ПУЛА (до реранка/отсечки), а не про top-limit:
    # см. docstring min_score.
    if min_score and min_score > 0 and not getattr(embedder, "use_sparse", True):
        pool_best = max((h["score"] for h in hits), default=0.0)
        if pool_best < min_score:
            return []
    if reranker is not None:
        try:
            ranked = reranker.rerank(query, hits, limit, exclude_ids=exclude_ids)
        except Exception as exc:  # noqa: BLE001 — реранк не должен ронять запрос
            # Сайдкар/модель/сеть упали: прод обязан отвечать, порядок остаётся RRF.
            log.warning("реранк недоступен (%s) — выдача остаётся в порядке RRF", exc)
            if work_ids is not None:
                hits = [h for h in hits if is_visible(h, work_ids)]
            return hits[:limit]
        if exclude_ids:
            # Реранк не имеет права воскресить исключённый врачом источник,
            # даже если его реализация забыла про фильтр (defense in depth).
            ranked = [h for h in ranked if h.get("guideline_id") not in exclude_ids]
        if work_ids is not None:
            # То же для персональной видимости статей (FR-36).
            ranked = [h for h in ranked if is_visible(h, work_ids)]
        return ranked[:limit]
    if work_ids is not None:
        hits = [h for h in hits if is_visible(h, work_ids)]
    return hits[:limit]


def count_points(client: QdrantClient) -> int:
    res = client.count(collection_name=COLLECTION, exact=True)
    return res.count


__all__ = [
    "COLLECTION",
    "IndexConfigError",
    "check_embedder_compatibility",
    "ensure_collection",
    "build_points",
    "upsert_points",
    "upsert_documents",
    "delete_sources",
    "hybrid_search",
    "is_visible",
    "count_points",
]
