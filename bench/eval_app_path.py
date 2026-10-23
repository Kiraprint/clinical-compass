#!/usr/bin/env python3
"""Качество RU/EN-поиска на РЕАЛЬНОМ продовом пути (app.services.kb.hybrid_search).

В отличие от run_bench.py (свой упрощённый harness) этот скрипт индексирует
корпус в Qdrant через `kb.upsert_documents` и ищет через `kb.hybrid_search` —
тот же код, что обслуживает /assistant/ask: NamedVector dense+sparse,
prefetch → RRF fusion. Меряет nDCG@3/Recall@3 (целевая метрика задачи),
nDCG@10/MRR@10, латентность запроса p50/p95, время индексации и память модели.

Данные (не в git, собираются bench/fetch_sources.py + build_dataset.py +
expand_queries.py): bench/data/corpus.jsonl (108 чанков), queries.jsonl
(55 запросов, 14 с явным lang; остальные делим по письменности).

Запуск (venv с torch/FlagEmbedding/sentence-transformers; qdrant-client):
    python bench/eval_app_path.py --profile bge-m3 --collection kb_eval_bge \
        --json-out bench/data/eval_app_bge_m3.json
Профили: hash | bge-m3 | e5 | frida. `--qdrant-url` по умолчанию локальный Qdrant.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import resource
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = HERE / "data"
sys.path.insert(0, str(ROOT / "api"))
sys.path.insert(0, str(ROOT / "core"))

# app.config валидирует секреты на импорте: для офлайн-замера хватит демо-флагов.
os.environ.setdefault("CC_ALLOW_DEMO_SECRETS", "true")
os.environ.setdefault("CC_QDRANT_COLLECTION", "kb_eval")
os.environ.setdefault("CC_EMBEDDER", "hash")

PROFILES = ("hash", "bge-m3", "e5", "frida")
CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)


# ---------- метрики ----------
def recall_at(ranked: list[str], qrels: list[str], k: int) -> float:
    return len(set(ranked[:k]) & set(qrels)) / max(1, len(qrels))


def ndcg_at(ranked: list[str], qrels: list[str], k: int) -> float:
    dcg = sum(1.0 / math.log2(i + 2) for i, d in enumerate(ranked[:k]) if d in qrels)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(min(len(qrels), k)))
    return dcg / idcg if idcg else 0.0


def mrr_at(ranked: list[str], qrels: list[str], k: int) -> float:
    for i, d in enumerate(ranked[:k]):
        if d in qrels:
            return 1.0 / (i + 1)
    return 0.0


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))
    return s[idx]


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0  # Linux: КиБ


def vram_mb() -> float:
    try:
        import torch

        if torch.cuda.is_available():
            return torch.cuda.max_memory_allocated() / 2**20
    except Exception:  # noqa: BLE001 — torch нет / CUDA выключена
        pass
    return 0.0


# ---------- кандидаты (альтернативы живут только здесь, не в проде) ----------
_ST_CACHE: dict[tuple[str, str], object] = {}


class SentenceTransformerDense:
    """dense через sentence-transformers + лексический hash-sparse.

    Раздельные инстансы для индекса и запроса: e5/frida требуют разных
    префиксов («passage:» / «query:»), а SPI приложения различает только текст.
    """

    def __init__(self, model_id: str, *, prefix: str = "", device: str = "auto") -> None:
        from sentence_transformers import SentenceTransformer

        from app.services.embeddings import HashEmbedder

        key = (model_id, device)
        if key not in _ST_CACHE:
            _ST_CACHE[key] = SentenceTransformer(model_id, device=None if device == "auto" else device)
        self._m = _ST_CACHE[key]
        self._prefix = prefix
        self._hash = HashEmbedder()
        self.use_sparse = False  # как прод-профиль bge-m3: сравнение в dense-only режиме
        dim = getattr(self._m, "get_embedding_dimension", None) or self._m.get_sentence_embedding_dimension
        self.dense_size = int(dim())

    def dense(self, text: str) -> list[float]:
        vec = self._m.encode([self._prefix + text], normalize_embeddings=True, show_progress_bar=False)
        return vec[0].tolist()

    def sparse(self, text: str) -> tuple[list[int], list[float]]:
        return self._hash.sparse(text)


def build_profile(profile: str, device: str, batch_size: int):
    """(embedder для индексации, embedder для запросов) — прод-классы где можно."""
    from app.services.embeddings import BgeM3Embedder, HashEmbedder

    if profile == "hash":
        emb = HashEmbedder()
        return emb, emb
    if profile == "bge-m3":
        emb = BgeM3Embedder("BAAI/bge-m3", device=device, batch_size=batch_size)
        return emb, emb
    if profile == "e5":
        model = "intfloat/multilingual-e5-large"
        return (
            SentenceTransformerDense(model, prefix="passage: ", device=device),
            SentenceTransformerDense(model, prefix="query: ", device=device),
        )
    if profile == "frida":
        model = "ai-forever/FRIDA"
        return (
            SentenceTransformerDense(model, prefix="search_document: ", device=device),
            SentenceTransformerDense(model, prefix="search_query: ", device=device),
        )
    raise SystemExit(f"неизвестный профиль {profile}; доступны {PROFILES}")


def load_data() -> tuple[list[dict], list[dict]]:
    corpus = [json.loads(line) for line in open(DATA / "corpus.jsonl", encoding="utf-8")]
    queries = [json.loads(line) for line in open(DATA / "queries.jsonl", encoding="utf-8")]
    missing = [
        q["id"]
        for q in queries
        for rel in q["qrels"]
        if rel not in {d["id"] for d in corpus}
    ]
    if missing:
        raise SystemExit(f"qrels ссылаются на отсутствующие чанки: {missing[:5]}")
    return corpus, queries


def lang_of(query: dict) -> str:
    if query.get("lang") in ("ru", "en"):
        return query["lang"]
    return "ru" if CYRILLIC.search(query["q"]) else "en"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", choices=PROFILES, required=True)
    ap.add_argument("--collection", default=None, help="изолированная коллекция Qdrant (пересоздаётся)")
    ap.add_argument("--qdrant-url", default=os.environ.get("CC_QDRANT_URL", "http://127.0.0.1:6333"))
    ap.add_argument("--device", default="auto", help="auto | cpu | cuda[:N]")
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--limit", type=int, default=10, help="top-k поиска (нужно >= 10 для nDCG@10)")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    if args.limit < 10:
        raise SystemExit("--limit должен быть >= 10")
    collection = args.collection or f"kb_eval_{args.profile.replace('-', '_')}"
    if collection in ("kb", "kb_test"):
        raise SystemExit("нельзя использовать боевую/тестовую коллекцию: укажите --collection")

    from qdrant_client import QdrantClient

    from app.services import kb

    baseline_rss = rss_mb()
    corpus, queries = load_data()
    docs = [
        {
            "guideline_id": d["id"],
            "source_title": d.get("source", ""),
            "topic": d.get("topic", ""),
            "text": d["text"],
        }
        for d in corpus
    ]

    t0 = time.perf_counter()
    idx_emb, qry_emb = build_profile(args.profile, args.device, args.batch_size)
    load_seconds = time.perf_counter() - t0
    model_rss = rss_mb()

    client = QdrantClient(url=args.qdrant_url, timeout=60)
    if client.collection_exists(collection):
        client.delete_collection(collection)
    # app.services.kb читает имя коллекции из settings на импорте модуля.
    kb.COLLECTION = collection

    t0 = time.perf_counter()
    kb.upsert_documents(client, idx_emb, docs)
    index_seconds = time.perf_counter() - t0
    points = kb.count_points(client)
    index_rss = rss_mb()

    # Прогрев: первый запрос платит за инициализацию CUDA/ядер.
    kb.hybrid_search(client, qry_emb, queries[0]["q"], limit=args.limit)

    rows: list[dict] = []
    latencies: list[float] = []
    for q in queries:
        t0 = time.perf_counter()
        hits = kb.hybrid_search(client, qry_emb, q["q"], limit=args.limit)
        latencies.append((time.perf_counter() - t0) * 1000.0)
        ranked = [h["guideline_id"] for h in hits]
        rows.append(
            {
                "id": q["id"],
                "lang": lang_of(q),
                "ndcg@3": ndcg_at(ranked, q["qrels"], 3),
                "recall@3": recall_at(ranked, q["qrels"], 3),
                "ndcg@10": ndcg_at(ranked, q["qrels"], 10),
                "mrr@10": mrr_at(ranked, q["qrels"], 10),
            }
        )

    def agg(group: str) -> dict:
        sub = [r for r in rows if group == "all" or r["lang"] == group]
        return {
            "queries": len(sub),
            "ndcg@3": statistics.mean(r["ndcg@3"] for r in sub),
            "recall@3": statistics.mean(r["recall@3"] for r in sub),
            "ndcg@10": statistics.mean(r["ndcg@10"] for r in sub),
            "mrr@10": statistics.mean(r["mrr@10"] for r in sub),
        }

    result = {
        "profile": args.profile,
        "collection": collection,
        "device": args.device,
        "corpus_chunks": len(docs),
        "points": points,
        "model_id": getattr(idx_emb, "model_id", None) or type(idx_emb).__name__,
        "dense_size": idx_emb.dense_size,
        "model_load_seconds": round(load_seconds, 2),
        "index_seconds": round(index_seconds, 2),
        "index_docs_per_second": round(len(docs) / index_seconds, 1) if index_seconds else None,
        "latency_ms_p50": round(percentile(latencies, 0.50), 1),
        "latency_ms_p95": round(percentile(latencies, 0.95), 1),
        "rss_mb": {
            "baseline": round(baseline_rss, 1),
            "after_model_load": round(model_rss, 1),
            "after_index": round(index_rss, 1),
        },
        "vram_mb_peak": round(vram_mb(), 1),
        "metrics": {g: agg(g) for g in ("ru", "en", "all")},
        "per_query": rows,
    }

    print(f"\n=== {args.profile} ({result['model_id']}, dense={idx_emb.dense_size}) ===")
    print(
        f"{'group':6s} {'n':>3s} {'nDCG@3':>7s} {'R@3':>6s} {'nDCG@10':>8s} {'MRR@10':>7s}"
    )
    for g in ("ru", "en", "all"):
        m = result["metrics"][g]
        print(f"{g:6s} {m['queries']:3d} {m['ndcg@3']:7.3f} {m['recall@3']:6.3f} {m['ndcg@10']:8.3f} {m['mrr@10']:7.3f}")
    print(
        f"latency p50/p95 = {result['latency_ms_p50']}/{result['latency_ms_p95']} мс; "
        f"индексация {index_seconds:.2f} с ({result['index_docs_per_second']} чанк/с); "
        f"загрузка модели {load_seconds:.1f} с; RSS peak {index_rss:.0f} МБ "
        f"(+{index_rss - baseline_rss:.0f} к базовой), VRAM peak {result['vram_mb_peak']:.0f} МБ"
    )

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"json: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
