#!/usr/bin/env python3
"""Пересборка коллекции KB в чанки CC_KB_CHUNK_CHARS из уже лежащих точек.

Зачем: реестр `kb_works` может быть потерян (пересозданная БД, ручной
`truncate`), а точки в Qdrant остались. `python -m app.enrichment reindex`
такой индекс восстановить не может — он переиндексирует работы из реестра.
Этот инструмент берёт ТЕКСТ уже проиндексированных чанков, режет его до
целевого размера и заново эмбеддит тем профилем, что задан в `CC_EMBEDDER`,
сохраняя payload (source_title, guideline_id, source, kind, year, source_url,
license, work_id) — провенанс не теряется.

Ограничения (осознанные):
- заголовок-провенанс от обогащения остаётся только в первом подчанке;
- исходный текст уже мог быть обрезан старым чанкером, т.е. это
  пере-нарезка существующего индекса, а не повторная выгрузка источника;
- seed-корпус (`kr-…`, `scale-…`, `drug-…`) не трогаем — его пересобирает
  `python -m app.kb_seed --reset` с теми же CC_KB_CHUNK_*.

Запуск (внутри контейнера api, где доступны ml/qdrant/postgres):

    CC_QDRANT_COLLECTION=kb_600 CC_EMBEDDER=remote-bge-m3 \
    CC_KB_CHUNK_CHARS=600 CC_KB_CHUNK_OVERLAP=120 \
    python -m tools.rechunk_collection --dry-run

    ... --source kb --target kb_600 --write

Предохранители: запись только в коллекцию, отличную от источника;
имена коллекций должны соответствовать `^kb(_[a-z0-9_]{1,40})?$`.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import uuid
from typing import Any, Iterable

from qdrant_client import QdrantClient, models as qm

from app.enrichment.index import chunk_text
from app.services import kb as kb_store
from app.services.embeddings import build_embedder

_NAME_RE = re.compile(r"^kb(_[a-z0-9_]{1,40})?$")
_HASH = lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest()  # noqa: E731
_SEED_PREFIXES = ("kr-", "scale-", "drug-")
_UPSERT_BATCH = 256


def _point_uuid(text: str) -> str:
    return str(uuid.UUID(hashlib.md5(text.encode("utf-8")).hexdigest()))


def _is_seed(payload: dict[str, Any]) -> bool:
    gid = str(payload.get("guideline_id", ""))
    return gid.startswith(_SEED_PREFIXES)


def _scroll(client: QdrantClient, collection: str) -> Iterable[qm.Record]:
    offset = None
    while True:
        records, offset = client.scroll(
            collection_name=collection,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        yield from records
        if offset is None:
            return


def _split(payload: dict[str, Any], size: int, overlap: int) -> list[dict[str, Any]]:
    text = str(payload.get("text") or "").strip()
    if not text:
        return []
    parts = chunk_text(text, size, overlap) if len(text) > size else [text]
    out: list[dict[str, Any]] = []
    for idx, part in enumerate(parts):
        part = part.strip()
        if not part:
            continue
        body = {k: v for k, v in payload.items() if k not in {"text", "text_hash", "chunk_index"}}
        body["text"] = part
        body["text_hash"] = _HASH(part)
        body["chunk_index"] = idx
        out.append(body)
    return out


def _points(docs: list[dict[str, Any]], embedder) -> list[qm.PointStruct]:
    texts = [d["text"] for d in docs]
    batch = getattr(embedder, "dense_batch", None)
    dense_vectors = batch(texts) if batch is not None else [embedder.dense(t) for t in texts]
    if len(dense_vectors) != len(texts):
        raise RuntimeError(f"эмбеддер вернул {len(dense_vectors)} векторов на {len(texts)} текстов")
    points: list[qm.PointStruct] = []
    for doc, dense in zip(docs, dense_vectors):
        indices, values = embedder.sparse(doc["text"])
        points.append(
            qm.PointStruct(
                id=_point_uuid(doc["text"]),
                vector={
                    kb_store.DENSE_NAME: dense,
                    kb_store.SPARSE_NAME: qm.SparseVector(indices=indices, values=values),
                },
                payload=doc,
            )
        )
    return points


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Пере-нарезка KB-коллекции в чанки CC_KB_CHUNK_*")
    ap.add_argument("--url", default="http://qdrant:6333")
    ap.add_argument("--source", default=kb_store.COLLECTION, help="откуда брать текст")
    ap.add_argument("--target", default=None, help="куда писать (по умолчанию CC_QDRANT_COLLECTION)")
    ap.add_argument("--dry-run", action="store_true", help="только посчитать")
    ap.add_argument("--write", action="store_true", help="записать (иначе dry-run)")
    ap.add_argument("--keep-seed", action="store_true", default=True, help="не переносить seed-чанки")
    args = ap.parse_args(argv)

    target = args.target or kb_store.COLLECTION
    for name in (args.source, target):
        if not _NAME_RE.match(name):
            print(f"ОТКАЗ: имя коллекции '{name}' не похоже на kb_*", file=sys.stderr)
            return 2
    if args.write and target == args.source:
        print("ОТКАЗ: источник и цель совпадают — пишите в отдельную коллекцию", file=sys.stderr)
        return 2

    size = 0
    from app.config import settings

    size, overlap = settings.kb_chunk_chars, settings.kb_chunk_overlap

    client = QdrantClient(url=args.url)
    total_in = total_out = skipped = 0
    docs: list[dict[str, Any]] = []
    for record in _scroll(client, args.source):
        payload = record.payload or {}
        total_in += 1
        if args.keep_seed and _is_seed(payload):
            skipped += 1
            continue
        parts = _split(payload, size, overlap)
        total_out += len(parts)
        docs.extend(parts)

    print(
        f"прочитано {total_in} точек, seed-пропущено {skipped}, "
        f"к переносу {len(docs)} чанков по {size}/{overlap}"
    )
    if not args.write:
        print("dry-run: ничего не записано")
        return 0
    if not docs:
        print("нечего переносить")
        return 0

    embedder = build_embedder()
    points = _points(docs, embedder)
    dense_size = len(points[0].vector[kb_store.DENSE_NAME])
    kb_store.ensure_collection(client, dense_size)
    # Qdrant режет тело запроса на 32 МБ: пишем пачками, а не одним upsert
    # (4763 чанка по 600 знаков — это ~88 МБ JSON).
    written = 0
    for start in range(0, len(points), _UPSERT_BATCH):
        batch = points[start : start + _UPSERT_BATCH]
        client.upsert(collection_name=target, points=batch, wait=True)
        written += len(batch)
    total = client.count(target).count
    print(f"записано {written} чанков в '{target}', всего в коллекции {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
