"""Качество поиска на ЖИВОМ индексе (docs/11 §9.4).

Голд-набор (`bench/data/qrels_600_full.jsonl`) хранит оценки по чанкам с
ключами `guideline_id#chunk_index`, а поиск возвращает point-id Qdrant. Поэтому
скрипт сначала прокручивает коллекцию и строит карту «композитный ключ →
point-id», иначе часть эталонных чанков молча теряется и метрики занижаются.

Считается на реальном пути `app.services.kb.hybrid_search` — тот же код, что
обслуживает `/assistant/ask`. Запуск там, где доступны сайдкар и Qdrant
(например, внутри контейнера `api`):

    docker cp bench/eval_live_index.py infra-api-1:/tmp/
    docker cp bench/data/qrels_600_full.jsonl infra-api-1:/tmp/
    docker exec infra-api-1 /app/.venv/bin/python /tmp/eval_live_index.py

Коллекция и эмбеддер берутся из настроек приложения (CC_QDRANT_COLLECTION,
CC_EMBEDDER) — то есть по умолчанию измеряется ровно прод-конфигурация.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time

from qdrant_client import QdrantClient

from app.config import settings
from app.services import embeddings, kb

DEFAULT_QRELS = "/tmp/qrels_600_full.jsonl"


def point_id_map(client: QdrantClient, collection: str) -> dict[str, str]:
    """Композитный ключ голда → реальный point-id коллекции."""
    idmap: dict[str, str] = {}
    offset = None
    while True:
        points, offset = client.scroll(
            collection, limit=256, offset=offset, with_payload=True, with_vectors=False
        )
        for p in points:
            payload = p.payload or {}
            gid, idx = payload.get("guideline_id"), payload.get("chunk_index")
            if gid is None or idx is None:
                continue
            idmap[f"{gid}#{int(idx)}"] = str(p.id)
        if offset is None:
            break
    return idmap


def score_hits(hits: list[str], rel: dict[str, int], strict: dict[str, int]) -> dict:
    """Hit@k (любая релевантная / строго grade=2), nDCG@3, MRR@10."""
    top = hits[:10]
    out: dict[str, float] = {}
    for k in (1, 3, 5, 10):
        out[f"hit{k}"] = 1.0 if any(h in rel for h in top[:k]) else 0.0
        out[f"strict_hit{k}"] = 1.0 if any(h in strict for h in top[:k]) else 0.0
    dcg = sum((2 ** rel[h] - 1) / math.log2(i + 2) for i, h in enumerate(top[:3]) if h in rel)
    ideal = sum(
        (2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(sorted(rel.values(), reverse=True)[:3])
    )
    out["ndcg3"] = dcg / ideal if ideal else 0.0
    out["mrr10"] = next((1.0 / (i + 1) for i, h in enumerate(top) if h in rel), 0.0)
    return out


def run(qrels_path: str, collection: str, rerank: bool) -> dict:
    client = QdrantClient(url=settings.qdrant_url, timeout=60)
    idmap = point_id_map(client, collection)
    embedder = embeddings.build_embedder()
    reranker = None
    if rerank:
        from app.services.reranker import build_reranker

        reranker = build_reranker()

    rows = [json.loads(line) for line in open(qrels_path, encoding="utf-8")]
    agg: dict[str, list[float]] = {}
    lat: list[float] = []
    used = 0
    total_grade2 = missing_grade2 = 0
    for row in rows:
        rel: dict[str, int] = {}
        strict: dict[str, int] = {}
        for key, grade in (row.get("grades") or {}).items():
            pid = idmap.get(key)
            if grade >= 2:
                total_grade2 += 1
                if pid is None:
                    missing_grade2 += 1
            if pid is None or grade < 1:
                continue
            rel[pid] = int(grade)
            if grade >= 2:
                strict[pid] = int(grade)
        if not rel:
            continue  # у запроса нет ни одного эталонного чанка в этом индексе
        t0 = time.perf_counter()
        hits = kb.hybrid_search(
            client,
            embedder,
            row["q"],
            limit=10,
            candidates=20 if reranker else None,
            reranker=reranker,
        )
        lat.append((time.perf_counter() - t0) * 1000)
        used += 1
        for name, value in score_hits([str(h["id"]) for h in hits], rel, strict).items():
            agg.setdefault(name, []).append(value)
    lat.sort()
    return {
        "collection": collection,
        "points": len(idmap),
        "queries_total": len(rows),
        "queries_used": used,
        "gold_grade2_keys": total_grade2,
        "gold_grade2_unresolved": missing_grade2,
        "coverage_grade2": round(1 - missing_grade2 / total_grade2, 4) if total_grade2 else None,
        "rerank": bool(reranker),
        **{k: round(statistics.mean(v), 4) for k, v in sorted(agg.items())},
        "latency_p50_ms": round(statistics.median(lat), 1),
        "latency_p95_ms": round(lat[int(len(lat) * 0.95) - 1], 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--qrels", default=DEFAULT_QRELS, help="jsonl с полем grades")
    ap.add_argument("--collection", default=settings.qdrant_collection)
    ap.add_argument("--rerank", action="store_true", help="включить CC_RERANKER")
    args = ap.parse_args()
    print(json.dumps(run(args.qrels, args.collection, args.rerank), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())