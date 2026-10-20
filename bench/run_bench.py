#!/usr/bin/env python3
"""Бенчмарк качества поиска по базе знаний: кандидаты-эмбеддеры на RU-EN корпусе по психиатрии.

Прогон: bench/run_bench.py [--model MODEL[:variant]] [--all] [--json]
Модели (через sentence-transformers / FlagEmbedding):
  - hash            : текущий HashEmbedder (feature-hashing, offline) — baseline
  - bge-m3          : BAAI/bge-m3 (dense 1024 + sparse + colbert)
  - user-bge-m3     : deepvk/USER-bge-m3 (RU-focused, dense 1024)
  - frida           : ai-forever/FRIDA (RU+EN, prefix-aware, 1.7B T5)
  - e5-multilingual : intfloat/multilingual-e5-large (1024)
  - qwen3-0.6b      : Qwen/Qwen3-Embedding-0.6B (multilingual top MTEB)
Харнесс: точный dense-поиск (косинус) + sparse (разреженные веса модели или
лексические хеши) слиянием RRF; метрики Recall@1/5/10, nDCG@10, MRR@10.

Принципиально: запускается отдельно от API/демо (не входит в докер-образ);
модели тянутся с HF при первом прогоне. CLI:
  bench/bin/python bench/run_bench.py --all --json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# чанкёр для больших текстов — берём из сборщика датасета
from build_dataset import _chunk  # noqa: E402

DATA = os.path.join(HERE, "data")

# ---------- метрики ----------
def recall_at(ranked: list[str], qrels: list[str], k: int) -> float:
    top = set(ranked[:k])
    return len(top.intersection(qrels)) / max(1, len(qrels))


def ndcg_at(ranked: list[str], qrels: list[str], k: int) -> float:
    rel = {d: 1 for d in qrels}
    dcg = 0.0
    for i, d in enumerate(ranked[:k]):
        if d in rel:
            dcg += 1.0 / math.log2(i + 2)
    # идеальный порядок: все релевантные впереди
    idcg = sum(1.0 / math.log2(i + 2) for i in range(min(len(qrels), k)))
    return dcg / idcg if idcg else 0.0


def mrr_at(ranked: list[str], qrels: list[str], k: int) -> float:
    for i, d in enumerate(ranked[:k]):
        if d in qrels:
            return 1.0 / (i + 1)
    return 0.0


# ---------- эмбеддеры ----------
# feature-hashing dense + лексический sparse — эталон «без модели», повторяет
# логику api/app/services/embeddings.py (не тянем api, чтобы не тащить его deps).
def _tokens(text: str) -> list[str]:
    import re

    lower = text.lower()
    words = re.findall(r"[a-zа-яё0-9][a-zа-яё0-9\-]{1,39}", lower)
    tokens = list(words)
    for i in range(len(words) - 2):
        tokens.append(" ".join(words[i : i + 3]))
    return tokens


def _hash_index(token: str, buckets: int) -> int:
    import hashlib

    return int(hashlib.md5(token.encode("utf-8")).hexdigest()[:8], 16) % buckets


class HashEmbedder:
    dense_size = 512

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            vec = [0.0] * self.dense_size
            for tok in _tokens(t):
                vec[_hash_index(tok, self.dense_size)] += 1.0
            norm = sum(v * v for v in vec) ** 0.5
            out.append([v / norm for v in vec] if norm else vec)
        return out

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return self.embed(texts)

    def sparse(self, texts):
        out = []
        for t in texts:
            scores: dict[int, float] = {}
            for tok in _tokens(t):
                idx = _hash_index(tok, 65536)
                scores[idx] = scores.get(idx, 0.0) + 1.0
            norm = sum(v * v for v in scores.values()) ** 0.5
            if norm:
                scores = {k: v / norm for k, v in scores.items()}
            out.append((list(scores.keys()), list(scores.values())))
        return out

    @property
    def name(self):
        return "hash"


class STEmbedder:
    """Обёртка над sentence-transformers: dense + (если есть) sparse."""

    def __init__(self, model_name: str, prefix_query: str = "", prefix_doc: str = ""):
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name)
        self._pq = prefix_query
        self._pd = prefix_doc
        self.dense_size = self._model.get_sentence_embedding_dimension()

    def embed(self, texts: list[str]) -> list[list[float]]:
        prefixed = [self._pd + t for t in texts]
        v = self._model.encode(prefixed, normalize_embeddings=True, show_progress_bar=False)
        return [x.tolist() for x in v]

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        prefixed = [self._pq + t for t in texts]
        v = self._model.encode(prefixed, normalize_embeddings=True, show_progress_bar=False)
        return [x.tolist() for x in v]

    def sparse(self, texts):  # лексический fallback (нет у ST-моделей)
        h = HashEmbedder()
        return h.sparse(texts)

    @property
    def name(self):
        return "st"


class BgeM3Embedder:
    """bge-m3 через FlagEmbedding: dense + подлинный sparse."""

    def __init__(self, model_name: str = "BAAI/bge-m3"):
        from FlagEmbedding import BGEM3FlagModel

        self._m = BGEM3FlagModel(model_name, use_fp16=False)
        self.dense_size = 1024

    def embed(self, texts):
        out = self._m.encode(texts, return_dense=True, return_sparse=True,
                             max_length=2048)
        return [x.tolist() for x in out["dense_vecs"]]

    def embed_queries(self, texts):
        return self.embed(texts)

    def sparse(self, texts):
        out = self._m.encode(texts, return_sparse=True, max_length=2048)
        # веса sparse — словарь {index: weight}
        return [(list(w.keys()), list(w.values())) for w in out["lexical_weights"]]

    @property
    def name(self):
        return "bge-m3"


# ---------- поиск ----------
def cosine(a, b):
    return sum(x * y for x, y in zip(a, b))


def rrf_score(ranked_lists: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    """RRF fusion нескольких ранжирований."""
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, doc in enumerate(ranked[:k]):
            scores[doc] = scores.get(doc, 0.0) + 1.0 / (60 + rank + 1)
    return sorted(scores.items(), key=lambda kv: -kv[1])


def retrieve(doc_emb, query_emb, doc_sparse, query_sparse, topk=30, variant="hybrid"):
    """dense / dense+sparse-RRF (hybrid) / dense+BM25-RRF (bm25)."""
    # dense
    dense_scores = []
    for i, de in enumerate(doc_emb):
        s = cosine(query_emb, de)
        dense_scores.append((i, s))
    dense_scores.sort(key=lambda kv: -kv[1])
    dense_ranked = [DOC_IDS[i] for i, _ in dense_scores[:topk]]
    if variant == "dense":
        return dense_ranked

    # sparse: векторное произведение разреженных весов
    qi, qv = query_sparse
    qmap = dict(zip(qi, qv))
    sparse_scores = []
    for i, (di, dv) in enumerate(doc_sparse):
        s = 0.0
        for idx, w in zip(di, dv):
            s += w * qmap.get(idx, 0.0)
        if s > 0:
            sparse_scores.append((i, s))
    # только реальные совпадения (s>0): нулевые спарс-доки = тии, они шумят RRF
    sparse_scores.sort(key=lambda kv: -kv[1])
    sparse_ranked = [DOC_IDS[i] for i, _ in sparse_scores[:topk]]
    if not sparse_ranked:
        return dense_ranked
    return [d for d, _ in rrf_score([dense_ranked, sparse_ranked], topk)]


# ---------- BM25: честный лексический sparse для всех моделей ----------
class BM25Index:
    """Okapi BM25 без внешних зависимостей (RU/EN, токенизация из _tokens)."""

    def __init__(self, corpus_texts: list[str]):
        self._toks = [_tokens(t) for t in corpus_texts]
        self._df: dict[str, int] = {}
        for toks in self._toks:
            for tok in set(toks):
                self._df[tok] = self._df.get(tok, 0) + 1
        self._n = len(corpus_texts)
        self._avgdl = sum(len(t) for t in self._toks) / max(1, self._n)
        self._k1, self._b = 1.5, 0.75

    def scores(self, query: str) -> list[float]:
        qf: dict[str, int] = {}
        for tok in _tokens(query):
            qf[tok] = qf.get(tok, 0) + 1
        out = [0.0] * self._n
        for i, dt in enumerate(self._toks):
            df: dict[str, int] = {}
            for tok in dt:
                df[tok] = df.get(tok, 0) + 1
            dl = len(dt) or 1
            s = 0.0
            for tok, cnt in qf.items():
                f = df.get(tok, 0)
                if not f:
                    continue
                n = self._df.get(tok, 0)
                idf = math.log(1 + (self._n - n + 0.5) / (n + 0.5))
                s += idf * cnt * (self._k1 + 1) * f / (f + self._k1 * (1 - self._b + self._b * dl / self._avgdl))
            out[i] = s
        return out

# ---------- основной прогон ----------
def _embed(embedder, corpus, queries):
    t0 = time.time()
    corpus_texts = [d["text"] for d in corpus]
    doc_emb = embedder.embed(corpus_texts)
    doc_sparse = embedder.sparse(corpus_texts)
    t_embed = time.time() - t0
    q_emb = embedder.embed_queries([q["q"] for q in queries])
    q_sparse = embedder.sparse([q["q"] for q in queries])
    return corpus_texts, doc_emb, doc_sparse, q_emb, q_sparse, t_embed


def _evaluate(corpus_texts, doc_emb, doc_sparse, q_emb, q_sparse, queries, variant):
    bm25 = BM25Index(corpus_texts) if variant == "bm25" else None
    agg = {"recall@1": [], "recall@5": [], "recall@10": [], "ndcg@10": [], "mrr@10": []}
    per_query = []
    for qi_idx, q in enumerate(queries):
        qrels = q["qrels"]
        if bm25 is not None:
            # dense + BM25 → RRF (все модели на одной лексической плоскости)
            sc = bm25.scores(q["q"])
            order = sorted(range(len(sc)), key=lambda i: -sc[i])
            bm25_ranked = [DOC_IDS[i] for i in order]
            dense_scores = [(i, cosine(q_emb[qi_idx], de)) for i, de in enumerate(doc_emb)]
            dense_scores.sort(key=lambda kv: -kv[1])
            dense_ranked = [DOC_IDS[i] for i, _ in dense_scores[:30]]
            ranked = [d for d, _ in rrf_score([dense_ranked, bm25_ranked], 30)]
        else:
            ranked = retrieve(doc_emb, q_emb[qi_idx], doc_sparse, q_sparse[qi_idx],
                              variant=variant)
        per_query.append({"id": q["id"], "ranked": ranked[:10], "qrels": qrels})
        for m, fn in (("recall@1", lambda r: recall_at(r, qrels, 1)),
                      ("recall@5", lambda r: recall_at(r, qrels, 5)),
                      ("recall@10", lambda r: recall_at(r, qrels, 10)),
                      ("ndcg@10", lambda r: ndcg_at(r, qrels, 10)),
                      ("mrr@10", lambda r: mrr_at(r, qrels, 10))):
            agg[m].append(fn(ranked))
    avg = {m: sum(v) / len(v) for m, v in agg.items()}
    return avg, per_query


def run_model(name: str, embedder, corpus, queries, variant: str = "hybrid") -> dict:
    corpus_texts, doc_emb, doc_sparse, q_emb, q_sparse, t_embed = _embed(embedder, corpus, queries)
    avg, per_query = _evaluate(corpus_texts, doc_emb, doc_sparse, q_emb, q_sparse, queries, variant)
    avg["embed_s"] = round(t_embed, 1)
    avg["model"] = name
    return avg, per_query


def run_all_variants(name: str, embedder, corpus, queries):
    """Одна загрузка/эмбеддинг модели → dense, hybrid, bm25."""
    corpus_texts, doc_emb, doc_sparse, q_emb, q_sparse, t_embed = _embed(embedder, corpus, queries)
    out = {}
    for v in ("dense", "hybrid", "bm25"):
        avg, _ = _evaluate(corpus_texts, doc_emb, doc_sparse, q_emb, q_sparse, queries, v)
        avg["embed_s"] = round(t_embed, 1)
        avg["model"] = name
        out[v] = avg
    return out


MODELS = {
    "hash": lambda: HashEmbedder(),
    "bge-m3": lambda: BgeM3Embedder(),
    "user-bge-m3": lambda: STEmbedder("deepvk/USER-bge-m3"),
    "frida": lambda: STEmbedder("ai-forever/FRIDA", prefix_query="search_query: ",
                                prefix_doc="search_document: "),
    "e5-multilingual": lambda: STEmbedder("intfloat/multilingual-e5-large",
                                          prefix_query="query: ", prefix_doc="passage: "),
    "qwen3-0.6b": lambda: STEmbedder("Qwen/Qwen3-Embedding-0.6B"),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="имя модели или all; формат name[:dense|hybrid|bm25]")
    ap.add_argument("--variant", default="hybrid", choices=["dense", "hybrid", "bm25"],
                    help="плоскость sparse-канала (dense = без sparse)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--topk", type=int, default=10)
    args = ap.parse_args()

    corpus = [json.loads(l) for l in open(os.path.join(DATA, "corpus.jsonl"), encoding="utf-8")]
    queries = [json.loads(l) for l in open(os.path.join(DATA, "queries.jsonl"), encoding="utf-8")]
    global DOC_IDS
    DOC_IDS = [d["id"] for d in corpus]

    if args.model in (None, "all"):
        selected = [(m, args.variant) for m in MODELS]
    elif ":" in args.model:
        nm, vv = args.model.split(":", 1)
        selected = [(nm, vv)]
    else:
        selected = [(args.model, args.variant)]
    results = {}
    for m, variant in selected:
        if m not in MODELS:
            print(f"{m}: неизвестная модель; доступны {list(MODELS)}")
            continue
        if variant == "all":
            try:
                emb = MODELS[m]()
                allv = run_all_variants(m, emb, corpus, queries)
                for v, avg in allv.items():
                    key = m if v == "hybrid" else f"{m}:{v}"
                    results[key] = avg
                    print(f"{key:22s} R@1={avg['recall@1']:.3f} R@5={avg['recall@5']:.3f} "
                          f"R@10={avg['recall@10']:.3f} nDCG@10={avg['ndcg@10']:.3f} "
                          f"MRR={avg['mrr@10']:.3f} embed={avg['embed_s']}s")
            except Exception as e:
                print(f"{m}:all{' '*12} FAILED: {e}")
                results[f"{m}:all"] = {"error": str(e)}
            continue
        key = m if variant == "hybrid" else f"{m}:{variant}"
        try:
            emb = MODELS[m]()
            avg, perq = run_model(m, emb, corpus, queries, variant=variant)
            results[key] = avg
            print(f"{key:22s} R@1={avg['recall@1']:.3f} R@5={avg['recall@5']:.3f} "
                  f"R@10={avg['recall@10']:.3f} nDCG@10={avg['ndcg@10']:.3f} "
                  f"MRR={avg['mrr@10']:.3f} embed={avg['embed_s']}s")
        except Exception as e:
            print(f"{key:22s} FAILED: {e}")
            results[key] = {"error": str(e)}

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    # вывод лучшего по nDCG@10
    ok = {k: v for k, v in results.items() if "error" not in v}
    if ok:
        best = max(ok.items(), key=lambda kv: kv[1]["ndcg@10"])
        print(f"\nЛучший по nDCG@10: {best[0]} ({best[1]['ndcg@10']:.3f})")


if __name__ == "__main__":
    main()