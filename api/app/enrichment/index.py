"""Запись обогащённых источников в Qdrant — производный индекс реестра.

Инварианты:
- чанки существуют только для kb_works.status='approved' (или auto-published
  гайдлайнов), поэтому «неодобренное» физически недоступно агенту;
- повторная индексация удаляет прежние точки работы по payload.work_id —
  старые редакции не сосуществуют с новыми;
- текст чанка несёт ссылку на первоисточник, чтобы цитата в ответе агента
  была проверяемой без отдельного поля в контракте RAG.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Sequence

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from app.models import KBWork
from app.services import kb as kb_store

ARTICLE_TOPIC = "НАУЧНЫЕ-СТАТЬИ"
GUIDELINE_TOPIC = "РЕКОМЕНДАЦИИ"

_APPRASIAL_TOOL_LABEL = {
    "amstar2": "AMSTAR 2 (по абстракту, экспериментально)",
    "rob2": "RoB 2",
    "robins_i": "ROBINS-I",
    "agree2": "AGREE II (для гайдлайнов ВОЗ не применялся)",
    "none": "инструмент не выбран",
}


def _point_id(text: str) -> str:
    return str(uuid.UUID(hashlib.md5(text.encode("utf-8")).hexdigest()))


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_text(text: str, size: int = 1800, overlap: int = 120) -> list[str]:
    """Абзацно-осознанное разбиение: не режем посреди абзаца без нужды."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        if len(paragraph) > size:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            step = max(size - overlap, 200)
            for start in range(0, len(paragraph), step):
                piece = paragraph[start : start + size].strip()
                if piece:
                    chunks.append(piece)
            continue
        candidate = f"{buffer}\n\n{paragraph}" if buffer else paragraph
        if len(candidate) <= size:
            buffer = candidate
        else:
            chunks.append(buffer)
            buffer = paragraph
    if buffer:
        chunks.append(buffer)
    # Убираем точные дубликаты (одинаковый текст → одинаковый point id).
    seen: set[str] = set()
    unique: list[str] = []
    for chunk in chunks:
        digest = _hash(chunk)
        if digest in seen:
            continue
        seen.add(digest)
        unique.append(chunk)
    return unique


def article_text(work: KBWork) -> str:
    """Тело статьи без заголовка/ссылки: их добавляет build_documents к каждому чанку."""
    meta = work.meta or {}
    design = work.design or {}
    appraisal = work.appraisal or {}
    doi = meta.get("doi") or ""
    pmid = meta.get("pmid") or work.external_id
    journal = meta.get("journal") or ""
    year = meta.get("year") or ""
    lines = [
        f"{journal}, {year}. Дизайн: {design.get('design', 'не определён')}. PMID {pmid}"
        + (f", DOI {doi}" if doi else "") + ".",
        "Абстракт: " + (meta.get("abstract") or "нет"),
    ]
    if meta.get("results"):
        lines.append("Результаты (из абстракта): " + str(meta["results"]))
    if appraisal:
        tool = _APPRASIAL_TOOL_LABEL.get(appraisal.get("tool", "none"), appraisal.get("tool", "—"))
        lines.append(f"Оценка методологии: {tool}. {appraisal.get('summary', '')}".strip())
    return "\n".join(line for line in lines if line.strip())


def article_header(work: KBWork) -> str:
    meta = work.meta or {}
    pmid = meta.get("pmid") or work.external_id
    doi = meta.get("doi") or ""
    ids = f"PMID {pmid}" + (f", DOI {doi}" if doi else "")
    return f"{work.title}\n{ids}. Источник: {meta.get('url') or ''}"


def guideline_text(work: KBWork, chunk: str) -> str:
    meta = work.meta or {}
    header = (
        f"{work.title}. ВОЗ, редакция {work.revision.split('#')[0]}. "
        f"Лицензия: {meta.get('license') or 'не подтверждена'}. "
        f"Источник: {meta.get('url') or ''}."
    )
    return f"{header}\n\n{chunk}"


def build_documents(
    work: KBWork, *, chunk_chars: int = 1800, chunk_overlap: int = 120
) -> list[dict]:
    """Чанки для индексации (чистая функция, тестируется без Qdrant)."""
    if work.kind == "article":
        header = article_header(work)
        chunks = [
            f"{header}\n\n{chunk}"
            for chunk in chunk_text(article_text(work), chunk_chars, chunk_overlap)
        ]
        topic = ARTICLE_TOPIC
    else:
        chunks = [
            guideline_text(work, chunk)
            for chunk in chunk_text(work.full_text or "", chunk_chars, chunk_overlap)
        ]
        topic = GUIDELINE_TOPIC
    meta = work.meta or {}
    return [
        {
            "guideline_id": work.guideline_id,
            "source_title": work.title,
            "topic": topic,
            "text": text,
            "chunk_index": index,
            "work_id": str(work.id),
            "source": work.source,
            "kind": work.kind,
            "year": meta.get("year"),
            "source_url": meta.get("url") or "",
            "license": meta.get("license") or "",
            "revision": work.revision,
        }
        for index, text in enumerate(chunks)
    ]


def remove_work_id(client: QdrantClient, work_id) -> None:
    client.delete(
        collection_name=kb_store.COLLECTION,
        points_selector=qm.FilterSelector(
            filter=qm.Filter(
                must=[qm.FieldCondition(key="work_id", match=qm.MatchValue(value=str(work_id)))]
            )
        ),
        wait=True,
    )


def remove_work(client: QdrantClient, work: KBWork) -> None:
    remove_work_id(client, work.id)


def remote_count(client: QdrantClient, work: KBWork) -> int:
    """Сколько чанков работы реально лежит в индексе (0 при отсутствии коллекции).

    Нужен для сверки после внешнего сброса (kb-seed --reset пересоздаёт
    коллекцию): БД помнит indexed_hash, а точек уже нет.
    """
    try:
        if not client.collection_exists(kb_store.COLLECTION):
            return 0
        return int(
            client.count(
                collection_name=kb_store.COLLECTION,
                count_filter=qm.Filter(
                    must=[
                        qm.FieldCondition(
                            key="work_id", match=qm.MatchValue(value=str(work.id))
                        )
                    ]
                ),
                exact=True,
            ).count
        )
    except Exception:  # noqa: BLE001 — недоступный Qdrant не должен валить прогон
        return 0


def _points_for(documents: Sequence[dict], embedder) -> list[qm.PointStruct]:
    points: list[qm.PointStruct] = []
    for doc in documents:
        text = doc["text"]
        indices, values = embedder.sparse(text)
        points.append(
            qm.PointStruct(
                id=_point_id(text),
                vector={
                    kb_store.DENSE_NAME: embedder.dense(text),
                    kb_store.SPARSE_NAME: qm.SparseVector(indices=indices, values=values),
                },
                payload={**doc, "text_hash": _hash(text)},
            )
        )
    return points


def index_work(
    client: QdrantClient,
    embedder,
    work: KBWork,
    *,
    chunk_chars: int = 1800,
    chunk_overlap: int = 120,
) -> int:
    """Заменяет точки работы: prepare → snapshot → delete → upsert.

    Эмбеддинги считаются до удаления: сбой эмбеддера не должен стирать
    действующую опубликованную версию. Прежние точки снимаются в память и
    возвращаются, если upsert упал — иначе неудачная замена оставила бы
    approved-версию без чанков.
    """
    kb_store.ensure_collection(client, embedder.dense_size)
    documents = build_documents(work, chunk_chars=chunk_chars, chunk_overlap=chunk_overlap)
    points = _points_for(documents, embedder)
    previous = _snapshot_work(client, work.id)
    remove_work(client, work)
    if not points:
        return 0
    try:
        client.upsert(collection_name=kb_store.COLLECTION, points=points, wait=True)
    except Exception:  # noqa: BLE001 — восстанавливаем действующую версию
        # Упавший upsert мог записать часть новых точек: сначала снимаем всё по
        # work_id, иначе индекс останется со смесью двух редакций.
        try:
            remove_work(client, work)
        except Exception:  # noqa: BLE001 — не маскируем исходную ошибку
            pass
        if previous:
            client.upsert(collection_name=kb_store.COLLECTION, points=previous, wait=True)
        raise
    return len(points)


def _snapshot_work(client: QdrantClient, work_id) -> list[qm.PointStruct]:
    """Точки работы в виде, пригодном для восстановления через upsert."""
    if not client.collection_exists(kb_store.COLLECTION):
        return []
    work_filter = qm.Filter(
        must=[qm.FieldCondition(key="work_id", match=qm.MatchValue(value=str(work_id)))]
    )
    points: list[qm.PointStruct] = []
    offset = None
    while True:
        batch, offset = client.scroll(
            collection_name=kb_store.COLLECTION,
            scroll_filter=work_filter,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        points.extend(qm.PointStruct(id=p.id, vector=p.vector, payload=p.payload) for p in batch)
        if offset is None:
            return points


def index_documents(client: QdrantClient, embedder, documents: Sequence[dict]) -> int:
    """Низкоуровневая запись (тесты/смоук): те же payload-контракты."""
    kb_store.ensure_collection(client, embedder.dense_size)
    client.upsert(
        collection_name=kb_store.COLLECTION,
        points=_points_for(documents, embedder),
        wait=True,
    )
    return len(documents)
