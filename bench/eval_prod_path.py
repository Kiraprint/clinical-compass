#!/usr/bin/env python3
"""Валидация победного стека НА ПРОДОВОМ ПУТИ (app.services.kb.hybrid_search).

Отличие от bench/eval_retrieval.py: там матрица считается in-memory (быстро,
но своя реализация RRF/реранка). Здесь — ровно тот код, что обслуживает
/assistant/ask: Qdrant NamedVector (dense+sparse → FusionQuery/RrfQuery по
CC_RRF_K) → реранк (app.services.reranker) → порог профиля, применённый в
hybrid_search ко всему пулу. Метрики должны совпасть с матрицей (допуск на
округления/переранжирование одинаковых скоров).

Запуск (600-набор: тот же корпус/qrels, что в отчёте §6.2):
  CC_EMBEDDER=bge-m3 CC_QDRANT_COLLECTION=kb_eval_600 \
  CC_RERANKER=BAAI/bge-reranker-v2-m3 CC_RETRIEVAL_CANDIDATES=20 \
  CC_RERANK_WINDOW=700 CC_EMBEDDER_DEVICE=cuda \
  .venv/bin/python bench/eval_prod_path.py --profile bge-m3 \
      --corpus bench/data/kb_corpus_600.jsonl \
      --qrels bench/data/qrels_600_full.jsonl \
      --json-out bench/data/eval_prod_bge.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "api"))
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(HERE))

os.environ.setdefault("CC_ALLOW_DEMO_SECRETS", "true")

import kb_bench as kbb  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="bge-m3")
    ap.add_argument("--corpus", default=str(kbb.CORPUS))
    ap.add_argument("--queries", default=str(kbb.QUERIES))
    ap.add_argument("--qrels", default=str(kbb.QRELS))
    ap.add_argument("--collection", default="")
    ap.add_argument("--reindex", action="store_true", help="пересобрать коллекцию с нуля")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    from qdrant_client import QdrantClient

    from app.config import settings

    from app.services import embeddings, kb, reranker as reranker_mod

    if args.collection:
        kb.COLLECTION = args.collection
    if args.reindex and QdrantClient(url=settings.qdrant_url).collection_exists(kb.COLLECTION):
        QdrantClient(url=settings.qdrant_url).delete_collection(kb.COLLECTION)

    corpus = kbb.load_corpus(Path(args.corpus))
    queries = [json.loads(x) for x in Path(args.queries).read_text(encoding="utf-8").splitlines() if x.strip()]
    qrels = {}
    if Path(args.qrels).exists():
        for line in Path(args.qrels).read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                qrels[rec["qid"]] = {k: int(v) for k, v in rec["grades"].items()}

    client = QdrantClient(url=settings.qdrant_url, timeout=60)
    embedder = embeddings.build_embedder(kind=args.profile)
    rr = reranker_mod.build_reranker(settings.reranker)
    # Тот же гейт «нет релевантного ответа», что в prod (agent.search_kb):
    # CC_KB_MIN_SCORE, иначе дефолт профиля. Применяется внутри hybrid_search
    # ко ВСЕМУ пулу ДО реранка — сравнивать hits[0]["score"] нельзя, после
    # реранка это logit кросс-энкодера, а не косинус профиля.
    min_score = (
        settings.kb_min_score
        if settings.kb_min_score is not None
        else embeddings.profile_default_min_score(args.profile)
    )

    if client.collection_exists(kb.COLLECTION):
        n = kb.count_points(client)
        print(f"коллекция {kb.COLLECTION}: {n} точек (переиспользуем)")
    else:
        docs = [
            {"guideline_id": r["guideline_id"], "source_title": r["source_title"],
             "topic": r.get("topic") or "", "text": r["text"]}
            for r in corpus
        ]
        # Пачками: один upsert 3362 чанков = 76 МБ JSON, Qdrant режет запрос на
        # 32 МБ (нарезка 600 знаков × 3.4 индекса).
        t0 = time.perf_counter()
        batch = 256
        for i in range(0, len(docs), batch):
            kb.upsert_documents(client, embedder, docs[i : i + batch])
        print(f"проиндексировано {len(docs)} чанков за {time.perf_counter() - t0:.1f}s")

    rankings: dict[str, list[str]] = {}
    lat: list[float] = []
    by_text = {r["text"]: r["id"] for r in corpus}
    import torch

    _ = kb.hybrid_search(client, embedder, "warmup", limit=5, candidates=settings.retrieval_candidates or None,
                         reranker=rr, min_score=min_score)
    torch.cuda.synchronize()
    torch.cuda.reset_peak_memory_stats()
    for q in queries:
        t0 = time.perf_counter()
        hits = kb.hybrid_search(
            client, embedder, q["q"], limit=10,
            candidates=settings.retrieval_candidates or None, reranker=rr,
            min_score=min_score,
        )
        lat.append(time.perf_counter() - t0)
        rankings[q["id"]] = [by_text.get(h["text"], h["guideline_id"]) for h in hits]

    stats = {
        "profile": args.profile,
        "collection": kb.COLLECTION,
        "reranker": settings.reranker,
        "candidates": settings.retrieval_candidates,
        "rrf_k": settings.rrf_k,
        "min_score": min_score,
        "refused": sum(1 for r in rankings.values() if not r),
        "p50_ms": kbb.percentile(lat, 0.5) * 1000,
        "p95_ms": kbb.percentile(lat, 0.95) * 1000,
        "vram_mb": torch.cuda.max_memory_allocated() / 2**20 if torch.cuda.is_available() else 0.0,
    }
    if qrels:
        stats["metrics"] = kbb.evaluate(rankings, qrels)
        m = stats["metrics"]
        print(f"{args.profile}+{settings.reranker} (gate {min_score}): nDCG@3={m['nDCG@3']:.3f} "
              f"Recall@3={m['Recall@3']:.3f} p50={stats['p50_ms']:.0f}ms p95={stats['p95_ms']:.0f}ms "
              f"refused={stats['refused']}")
    stats["rankings"] = rankings
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(stats, ensure_ascii=False, indent=1))
        print(f"→ {args.json_out}")


if __name__ == "__main__":
    main()
