#!/usr/bin/env python3
"""Латентность retrieval-стека на CPU с ограничением потоков (`OMP_NUM_THREADS=4`).

Прод-контейнер API — CPU-only (torch в образ не входит), поэтому для решения
«включать ли нейросетевой профиль в проде» нужны CPU-числа, а не GPU.

Меряется по одному устройству за процесс (память/потоки не смешиваются):

  OMP_NUM_THREADS=4 <venv>/python bench/cpu_latency.py --model bge-m3
  OMP_NUM_THREADS=4 <venv>/python bench/cpu_latency.py --reranker bge-reranker-v2-m3
  OMP_NUM_THREADS=4 <venv>/python bench/cpu_latency.py --reranker qwen3-reranker-0.6b

Пишет JSON в bench/data/cpu_<slug>.json (или --json-out).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_bench as kbb  # noqa: E402

EMBEDDERS = {
    "bge-m3": ("BAAI/bge-m3", 1024, ""),
    "qwen3-0.6b": ("Qwen/Qwen3-Embedding-0.6B", 1024, ""),
    "user-bge-m3": ("deepvk/USER-bge-m3", 1024, ""),
    "e5-large-instruct": ("intfloat/multilingual-e5-large-instruct", 1024, ""),
    "frida": ("ai-forever/FRIDA", 1024, ""),
}
RERANKERS = {
    "bge-reranker-v2-m3": "BAAI/bge-reranker-v2-m3",
    "jina-reranker-v2": "jinaai/jina-reranker-v2-base-multilingual",
    "qwen3-reranker-0.6b": "Qwen/Qwen3-Reranker-0.6B",
}


def stats(values: list[float]) -> dict:
    return {
        "n": len(values),
        "p50_ms": round(1000 * kbb.percentile(values, 0.5), 1),
        "p95_ms": round(1000 * kbb.percentile(values, 0.95), 1),
        "p99_ms": round(1000 * kbb.percentile(values, 0.99), 1),
        "mean_ms": round(1000 * sum(values) / len(values), 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="", help="профиль эмбеддера из EMBEDDERS")
    ap.add_argument("--reranker", default="", help="ключ из RERANKERS")
    ap.add_argument("--queries", default=str(kbb.QUERIES))
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--threads", type=int, default=int(os.getenv("OMP_NUM_THREADS", "4")))
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    import torch

    torch.set_num_threads(args.threads)
    queries = [json.loads(line) for line in Path(args.queries).read_text(encoding="utf-8").splitlines() if line.strip()]
    queries = queries[: args.limit]
    out: dict = {"threads": args.threads, "torch": torch.__version__, "cuda": torch.cuda.is_available()}

    if args.model:
        from sentence_transformers import SentenceTransformer

        model_id, dim, prefix = EMBEDDERS[args.model]
        t0 = time.perf_counter()
        st = SentenceTransformer(model_id, device="cpu")
        out["load_s"] = round(time.perf_counter() - t0, 2)
        st.encode(["загрузка"], normalize_embeddings=True, batch_size=1)
        lat = []
        for q in queries:
            t = time.perf_counter()
            st.encode([prefix + q["q"]], normalize_embeddings=True, batch_size=1)
            lat.append(time.perf_counter() - t)
        out["embed_query"] = stats(lat)
        out["embedder"] = args.model
        out["dense"] = dim
        slug = f"cpu_{args.model}"
    elif args.reranker:
        from sentence_transformers import CrossEncoder

        model_id = RERANKERS[args.reranker]
        t0 = time.perf_counter()
        ce = CrossEncoder(model_id, device="cpu")
        out["load_s"] = round(time.perf_counter() - t0, 2)
        corpus = [json.loads(line)["text"] for line in Path(kbb.CORPUS).read_text(encoding="utf-8").splitlines() if line.strip()]
        out["reranker"] = args.reranker
        for depth in (20, 50):
            lat = []
            for q in queries:
                cand = corpus[:depth]
                t = time.perf_counter()
                if args.reranker == "qwen3-reranker-0.6b":
                    pairs = [(q["q"], c[:600]) for c in cand]
                    ce.predict(pairs, batch_size=4, show_progress_bar=False)
                else:
                    ce.predict([(q["q"], c[:600]) for c in cand], batch_size=4, show_progress_bar=False)
                lat.append(time.perf_counter() - t)
            out[f"rerank_{depth}"] = stats(lat)
        slug = f"cpu_rerank_{args.reranker}"
    else:
        raise SystemExit("нужен --model или --reranker")

    path = Path(args.json_out) if args.json_out else kbb.DATA / f"{slug}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
