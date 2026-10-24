#!/usr/bin/env python3
"""Матрица retrieval-стеков на РЕАЛЬНОМ корпусе БЗ (998 чанков) и общих qrels.

Что делает:
1. Грузит снимок корпуса (bench/data/kb_corpus.jsonl) и запросы (kb_queries.jsonl).
2. Векторизует корпус на GPU (кэш bench/data/cache/<slug>.npz), ищет по стекам:
   dense / dense+sparse RRF (k настраивается) / +ColBERT (bge-m3) / +cross-encoder
   реранк top-N кандидатов.
3. Считает nDCG@3/5/10, Recall@1/3/5/10 (grade≥1 и strict grade=2), MRR@10,
   латентность p50/p95 на запрос и пиковую VRAM.
4. `--dump-pools` — выгружает пул кандидатов (объединение top-C всех стеков)
   для разметки асессором (bench/grade_kb_qrels.py).

Запуск (GPU-venv с torch/FlagEmbedding/sentence-transformers):
  /path/to/bench/.venv/bin/python bench/eval_retrieval.py --stacks all \
      --qrels bench/data/kb_qrels.jsonl --json-out bench/data/eval_matrix.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for extra in (ROOT / "api", ROOT / "core"):
    sys.path.insert(0, str(extra))
sys.path.insert(0, str(HERE))

os.environ.setdefault("CC_ALLOW_DEMO_SECRETS", "true")

import numpy as np  # noqa: E402

import kb_bench as kbb  # noqa: E402

CACHE = HERE / "data" / "cache"

# ---------------------------------------------------------------- модели
# Единый интерфейс: encode_passages(list[str]) -> (N,D) float32, нормированные;
# encode_query(str) -> (D,); опционально sparse_weights(text) -> dict[str,float],
# colbert_passages(list[str]) -> list[np.ndarray] (T,D).
class STEmbedder:
    """sentence-transformers модель с префиксами запрос/пассаж."""

    kind = "st"

    def __init__(self, model_id: str, *, query_prefix: str = "", passage_prefix: str = "",
                 max_length: int = 512, trust_remote_code: bool = False) -> None:
        from sentence_transformers import SentenceTransformer

        self.model_id = model_id
        self.query_prefix = query_prefix
        self.passage_prefix = passage_prefix
        self.max_length = max_length
        self._m = SentenceTransformer(model_id, device="cuda", trust_remote_code=trust_remote_code)
        self._m.max_seq_length = max_length

    def encode_passages(self, texts: list[str], batch_size: int = 16) -> np.ndarray:
        prefix = self.passage_prefix
        out = self._m.encode(
            [prefix + t for t in texts], batch_size=batch_size, convert_to_numpy=True,
            normalize_embeddings=True, show_progress_bar=False,
        )
        return out.astype(np.float32)

    def encode_query(self, text: str) -> np.ndarray:
        return self._m.encode(
            [self.query_prefix + text], convert_to_numpy=True,
            normalize_embeddings=True, show_progress_bar=False,
        )[0].astype(np.float32)


class BgeM3:
    """FlagEmbedding BGEM3FlagModel: dense + lexical sparse + ColBERT."""

    kind = "bge-m3"

    def __init__(self, model_id: str = "BAAI/bge-m3") -> None:
        from FlagEmbedding import BGEM3FlagModel

        self.model_id = model_id
        self._m = BGEM3FlagModel(model_id, use_fp16=True, device="cuda")

    def dense_passages(self, texts: list[str], batch_size: int = 8) -> np.ndarray:
        out = self._m.encode(texts, batch_size=batch_size, max_length=1024,
                             return_dense=True, return_sparse=False, return_colbert_vecs=False)
        v = np.asarray(out["dense_vecs"], dtype=np.float32)
        return _l2(v)

    def encode_query(self, text: str) -> np.ndarray:
        out = self._m.encode([text], max_length=1024, return_dense=True,
                             return_sparse=False, return_colbert_vecs=False)
        return _l2(np.asarray(out["dense_vecs"], dtype=np.float32))[0]

    def sparse_weights(self, texts: list[str], batch_size: int = 8) -> list[dict[str, float]]:
        out = self._m.encode(texts, batch_size=batch_size, max_length=1024,
                             return_dense=False, return_sparse=True, return_colbert_vecs=False)
        return [{k: float(v) for k, v in w.items()} for w in out["lexical_weights"]]

    def colbert_passages(self, texts: list[str], batch_size: int = 4) -> list[np.ndarray]:
        out = self._m.encode(texts, batch_size=batch_size, max_length=1024,
                             return_dense=False, return_sparse=False, return_colbert_vecs=True)
        return [np.asarray(v, dtype=np.float16) for v in out["colbert_vecs"]]

    def colbert_query(self, text: str) -> np.ndarray:
        out = self._m.encode([text], max_length=1024, return_dense=False,
                             return_sparse=False, return_colbert_vecs=True)
        return np.asarray(out["colbert_vecs"][0], dtype=np.float32)


def _l2(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    n[n == 0] = 1.0
    return v / n


# ------------------------------------------------------- обёртки реранкеров
class CrossEncoderReranker:
    def __init__(
        self,
        model_id: str,
        *,
        trust_remote_code: bool = False,
        max_length: int = 512,
        chars: int = 0,
    ) -> None:
        from sentence_transformers import CrossEncoder

        self.name = model_id
        # chars>0 — обрезать фрагмент перед парой: цена CE растёт с длиной, а
        # при max_length=512 хвост 1800-знакового чанка всё равно отбрасывается.
        self.chars = chars
        self._m = CrossEncoder(model_id, device="cuda", trust_remote_code=trust_remote_code,
                               max_length=max_length)

    def score(self, query: str, texts: list[str]) -> np.ndarray:
        cut = [t[: self.chars] if self.chars else t for t in texts]
        return np.asarray(self._m.predict([(query, t) for t in cut], show_progress_bar=False))


class Qwen3Reranker:
    """Qwen3-Reranker: логит «yes» против «no» по официальной рецептуре карточки."""

    def __init__(self, model_id: str = "Qwen/Qwen3-Reranker-0.6B", max_length: int = 2048) -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.name = model_id
        self._torch = torch
        self._tok = AutoTokenizer.from_pretrained(model_id, padding_side="left")
        self._m = AutoModelForCausalLM.from_pretrained(model_id, torch_dtype=torch.float16).cuda().eval()
        self._m.config.pad_token_id = self._tok.pad_token_id
        self._max = max_length
        self._yes = self._tok.convert_tokens_to_ids("yes")
        self._no = self._tok.convert_tokens_to_ids("no")
        self._prefix = (
            "<|im_start|>system\nJudge whether the Document meets the requirements based on "
            "the Query and the Instruct provided. Note that the answer can only be \"yes\" or "
            "\"no\".<|im_end|>\n<|im_start|>user\n"
        )
        self._suffix = "<|im_end|>\n<|im_start|>assistant\n"
        self._instruct = "Given a clinical question, retrieve relevant passages that answer the question"

    def score(self, query: str, texts: list[str]) -> np.ndarray:
        import torch

        # Микро-батчи: один forward на 100 документов по 2048 токенов не влезает
        # в 24 ГБ рядом с эмбеддером (OOM 11.9 ГБ), а по 8 — стабильно.
        out: list[float] = []
        step = 8
        for i in range(0, len(texts), step):
            pairs = [
                f"<Instruct>: {self._instruct}\n<Query>: {query}\n<Document>: {t[:2000]}"
                for t in texts[i : i + step]
            ]
            enc = self._tok([self._prefix + p + self._suffix for p in pairs], padding=True,
                            truncation=True, max_length=self._max, return_tensors="pt").to("cuda")
            with torch.no_grad():
                logits = self._m(**enc).logits[:, -1, :]
            out.extend((logits[:, self._yes] - logits[:, self._no]).float().cpu().tolist())
            del enc, logits
        return np.asarray(out, dtype=np.float32)


RERANKERS = {
    "bge-reranker-v2-m3": ("ce", "BAAI/bge-reranker-v2-m3", {}),
    "jina-reranker-v2": ("ce", "jinaai/jina-reranker-v2-base-multilingual", {"trust_remote_code": True}),
    "qwen3-reranker-0.6b": ("qwen", "Qwen/Qwen3-Reranker-0.6B", {}),
    "dity-ru-msmarco": ("ce", "DiTy/cross-encoder-russian-msmarco", {}),
}

EMBEDDERS: dict[str, dict] = {
    "bge-m3": {"kind": "bge-m3"},
    "user-bge-m3": {"kind": "st", "model": "deepvk/USER-bge-m3", "max_length": 1024},
    "frida": {"kind": "st", "model": "ai-forever/FRIDA",
              "query_prefix": "search_query: ", "passage_prefix": "search_document: ",
              "max_length": 512},
    "e5-large-instruct": {"kind": "st", "model": "intfloat/multilingual-e5-large-instruct",
                          "query_prefix": "Instruct: Given a clinical question, retrieve passages "
                                          "that answer the question\nQuery: ",
                          "max_length": 512},
    "qwen3-0.6b": {"kind": "st", "model": "Qwen/Qwen3-Embedding-0.6B",
                   "query_prefix": "Instruct: Given a clinical question, retrieve passages that "
                                   "answer the question\nQuery: ", "max_length": 512},
    "qwen3-4b": {"kind": "st", "model": "Qwen/Qwen3-Embedding-4B",
                 "query_prefix": "Instruct: Given a clinical question, retrieve passages that "
                                 "answer the question\nQuery: ", "max_length": 512},
}
HASH_SPEC = {"kind": "hash"}


def load_embedder(name: str):
    if name == "hash":
        return kbb.HashEmbedderLite()
    spec = EMBEDDERS[name]
    if spec["kind"] == "bge-m3":
        return BgeM3(spec.get("model", "BAAI/bge-m3"))
    return STEmbedder(spec["model"], query_prefix=spec.get("query_prefix", ""),
                      passage_prefix=spec.get("passage_prefix", ""),
                      max_length=spec.get("max_length", 512))


def embedder_cache_path(name: str) -> Path:
    return CACHE / f"{name}.npz"


def build_dense(name: str, corpus: list[dict], *, need_sparse: bool, need_colbert: bool,
                batch_size: int) -> tuple[np.ndarray, object | None, list | None]:
    """Dense-матрица корпуса (кэш на диск) + sparse-веса + ColBERT-векторы."""
    emb = load_embedder(name)
    texts = [r["text"] for r in corpus]
    path = embedder_cache_path(name)
    if path.exists():
        data = np.load(path, allow_pickle=True)
        dense = data["dense"]
    else:
        CACHE.mkdir(parents=True, exist_ok=True)
        if name == "hash":
            dense = np.asarray([emb.dense(t) for t in texts], dtype=np.float32)
        else:
            dense = emb.encode_passages(texts, batch_size=batch_size)
        np.savez_compressed(path, dense=dense)
    sparse = None
    colbert = None
    if need_sparse and name == "bge-m3":
        sp_path = CACHE / f"{name}.sparse.json"
        if sp_path.exists():
            sparse = json.loads(sp_path.read_text())
        else:
            sparse = emb.sparse_weights(texts, batch_size=batch_size)
            sp_path.write_text(json.dumps(sparse))
    if need_colbert and name == "bge-m3":
        cb_path = CACHE / f"{name}.colbert.npz"
        if cb_path.exists():
            loaded = np.load(cb_path, allow_pickle=True)["vecs"]
            colbert = list(loaded)
        else:
            colbert = emb.colbert_passages(texts)
            np.savez_compressed(cb_path, vecs=np.array(colbert, dtype=object))
    return dense, sparse, colbert


# ---------------------------------------------------------------- поиск
def dense_scores(dense: np.ndarray, qvec: np.ndarray) -> np.ndarray:
    return dense @ qvec


def sparse_dicts(spec: str, texts: list[str], emb) -> list[dict[str, float]]:
    if spec == "hash":
        h = emb
        out = []
        for t in texts:
            idx, vals = h.sparse(t)
            out.append({str(i): v for i, v in zip(idx, vals)})
        return out
    raise ValueError(spec)


def sparse_scores(postings: list[dict[str, float]], qweights: dict[str, float]) -> np.ndarray:
    s = np.zeros(len(postings), dtype=np.float32)
    for i, p in enumerate(postings):
        for tok, w in qweights.items():
            v = p.get(tok)
            if v:
                s[i] += w * v
    return s


def colbert_scores(chunk_vecs: list[np.ndarray], qvec: np.ndarray) -> np.ndarray:
    q = qvec  # (Tq, D)
    out = np.full(len(chunk_vecs), -1e4, dtype=np.float32)
    for i, cv in enumerate(chunk_vecs):
        if cv is None or len(cv) == 0:
            continue
        sim = q @ cv.T.astype(np.float32)  # (Tq, Td)
        out[i] = float(sim.max(axis=1).mean())
    return out


def rrf_fuse(channels: list[np.ndarray], k: int, top: int) -> np.ndarray:
    """RRF по рангам каналов (каждый — массив score по всему корпусу)."""
    n = len(channels[0])
    fused = np.zeros(n, dtype=np.float32)
    for scores in channels:
        order = np.argsort(-scores)[:top]
        for rank, idx in enumerate(order, start=1):
            fused[idx] += 1.0 / (k + rank)
    return fused


def top_ids(scores: np.ndarray, corpus: list[dict], n: int) -> list[str]:
    order = np.argsort(-scores)[:n]
    return [corpus[i]["id"] for i in order]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stacks", default="all", help="имя стека | all | список через запятую")
    ap.add_argument("--corpus", default=str(kbb.CORPUS))
    ap.add_argument("--queries", default=str(kbb.QUERIES))
    ap.add_argument("--qrels", default=str(kbb.QRELS))
    ap.add_argument("--candidates", type=int, default=100, help="глубина каналов/пула")
    ap.add_argument("--rrf-k", type=int, default=60)
    ap.add_argument("--rerank-depth", type=int, default=50)
    ap.add_argument("--rerank-chars", type=int, default=0, help="обрезать фрагмент перед CE (0 — целиком)")
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--dump-pools", default="", help="путь JSONL для пула кандидатов")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--only-queries", default="", help="файл с id запросов (по одному в строке)")
    args = ap.parse_args()

    corpus = kbb.load_corpus(Path(args.corpus))
    queries = [json.loads(x) for x in Path(args.queries).read_text(encoding="utf-8").splitlines() if x.strip()]
    if args.only_queries:
        keep = {x.strip() for x in Path(args.only_queries).read_text().split() if x.strip()}
        queries = [q for q in queries if q["id"] in keep]
    qrels = {}
    if Path(args.qrels).exists():
        for line in Path(args.qrels).read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                qrels[rec["qid"]] = {k: int(v) for k, v in rec["grades"].items()}

    stacks = define_stacks(args)
    selected = list(stacks) if args.stacks == "all" else [s.strip() for s in args.stacks.split(",")]

    import torch

    results: dict[str, dict] = {}
    pools: dict[str, set[str]] = {q["id"]: set() for q in queries}
    models: dict[str, object] = {}

    for sname in selected:
        spec = stacks[sname]
        name = spec["embedder"]
        if name not in models:
            models[name] = load_embedder(name)
        emb = models[name]
        need_sparse = bool(spec.get("sparse"))
        need_colbert = bool(spec.get("colbert"))
        dense, sparse, colbert = build_dense(
            name, corpus, need_sparse=need_sparse, need_colbert=need_colbert,
            batch_size=args.batch_size,
        )
        texts = [r["text"] for r in corpus]
        hash_postings = None
        if need_sparse and name != "bge-m3":
            hash_postings = sparse_dicts(name, texts, emb)

        reranker = None
        if spec.get("reranker"):
            kind, model_id, kwargs = RERANKERS[spec["reranker"]]
            t0 = time.perf_counter()
            if kind == "ce":
                reranker = CrossEncoderReranker(model_id, chars=args.rerank_chars, **kwargs)
            else:
                reranker = Qwen3Reranker(model_id)
            print(f"[{sname}] reranker {model_id} loaded in {time.perf_counter() - t0:.1f}s", file=sys.stderr)

        # прогрев: первый вызов грузит ядра CUDA и инициализирует кэши — в латентность не идёт
        _ = emb.encode_query("warmup")
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
        rankings: dict[str, list[str]] = {}
        lat: list[float] = []
        for q in queries:
            t0 = time.perf_counter()
            qv = emb.encode_query(q["q"])
            depth = int(spec.get("rerank_depth", args.rerank_depth))
            # Для реранк-стека пул = глубина реранка (свип глубины должен менять цену),
            # для прочих — обычная глубина каналов.
            cand = depth if spec.get("reranker") else args.candidates
            rrf_k = int(spec.get("rrf_k", args.rrf_k))
            if need_sparse:
                if name == "bge-m3":
                    sp = sparse_scores(sparse, emb.sparse_weights([q["q"]])[0])
                else:
                    idx, vals = emb.sparse(q["q"])
                    sp = sparse_scores(hash_postings, {str(i): v for i, v in zip(idx, vals)})
                scores = rrf_fuse([dense_scores(dense, qv), sp], k=rrf_k, top=cand)
            else:
                scores = dense_scores(dense, qv).astype(np.float32)
            order = np.argsort(-scores)[:cand]
            if spec.get("colbert"):
                # третий канал ColBERT: max-sim рескоринг кандидатов RRF(dense,sparse)
                cb = colbert_scores([colbert[i] for i in order], emb.colbert_query(q["q"]))
                order = order[np.argsort(-cb)]
            if reranker is not None:
                cand_texts = [texts[i] for i in order]
                rs = reranker.score(q["q"], cand_texts)
                ce_order = order[np.argsort(-rs)]
                if spec.get("blend"):
                    # RRF порядков dense и реранкера: кросс-энкодер на длинных
                    # чанках портит голову выдачи, слияние сохраняет оба сигнала.
                    bk = float(spec.get("blend_k", 10))
                    dense_rank = np.arange(len(order), dtype=np.float64)
                    ce_rank = np.argsort(np.argsort(-rs)).astype(np.float64)
                    order = order[np.argsort(-(1.0 / (bk + dense_rank) + 1.0 / (bk + ce_rank)))]
                else:
                    order = ce_order
            ranked = [corpus[i]["id"] for i in order[: args.limit]]
            rankings[q["id"]] = ranked
            lat.append(time.perf_counter() - t0)
            pools[q["id"]].update(corpus[i]["id"] for i in order[:depth])
        vram_peak = torch.cuda.max_memory_allocated() / 2**20 if torch.cuda.is_available() else 0.0
        vram_res = torch.cuda.memory_allocated() / 2**20 if torch.cuda.is_available() else 0.0
        vram = vram_peak
        stats = {"p50_ms": 1000 * kbb.percentile(lat, 0.5), "p95_ms": 1000 * kbb.percentile(lat, 0.95),
                 "p99_ms": 1000 * kbb.percentile(lat, 0.99),
                 "mean_ms": 1000 * sum(lat) / len(lat), "vram_mb": vram_peak,
                 "vram_resident_mb": vram_res}
        entry = {"stack": sname, "spec": {k: v for k, v in spec.items()}, "latency": stats,
                 "rankings": rankings}
        if qrels:
            entry["metrics"] = kbb.evaluate(rankings, qrels)
            entry["metrics_covered"] = kbb.evaluate_covered(rankings, qrels)
            entry["per_query"] = kbb.per_query_report(rankings, qrels)
        results[sname] = entry
        if qrels:
            m = entry["metrics"]
            print(f"{sname:44s} nDCG@3={m['nDCG@3']:.3f} R@3={m['Recall@3']:.3f} "
                  f"R2@3={m['Recall2@3']:.3f} p50={stats['p50_ms']:.0f}ms "
                  f"p95={stats['p95_ms']:.0f}ms VRAM={vram:.0f}MB")
        else:
            print(f"{sname:44s} p50={stats['p50_ms']:.0f}ms p95={stats['p95_ms']:.0f}ms VRAM={vram:.0f}MB")
        torch.cuda.empty_cache()

    if args.dump_pools:
        Path(args.dump_pools).write_text(
            "\n".join(json.dumps({"qid": qid, "candidates": sorted(ids)}) for qid, ids in pools.items()),
            encoding="utf-8",
        )
        print(f"пул кандидатов → {args.dump_pools} ({sum(len(v) for v in pools.values())} пар)")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, ensure_ascii=False, indent=1))
        print(f"результаты → {args.json_out}")


def define_stacks(args) -> dict[str, dict]:
    """Именованные стеки. `all` — полный замер для отчёта."""
    stacks = {
        # --- baseline: что в проде сейчас ---
        "hash-dense+sparse": {"embedder": "hash", "sparse": True, "rrf_k": 60},
        # --- эмбеддеры, dense-only ---
        "bge-m3-dense": {"embedder": "bge-m3", "sparse": False},
        "user-bge-m3-dense": {"embedder": "user-bge-m3", "sparse": False},
        "frida-dense": {"embedder": "frida", "sparse": False},
        "e5-large-instruct-dense": {"embedder": "e5-large-instruct", "sparse": False},
        "qwen3-0.6b-dense": {"embedder": "qwen3-0.6b", "sparse": False},
        "qwen3-4b-dense": {"embedder": "qwen3-4b", "sparse": False},
        # --- гибрид bge-m3 ---
        "bge-m3-dense+sparse": {"embedder": "bge-m3", "sparse": True},
        "bge-m3-dense+sparse+colbert": {"embedder": "bge-m3", "sparse": True, "colbert": True},
        # --- реранк пула: база × кросс-энкодер (глубина пула — --rerank-depth) ---
        "bge-m3-dense+rerank-bge": {"embedder": "bge-m3", "reranker": "bge-reranker-v2-m3"},
        "bge-m3-dense+rerank-dity": {"embedder": "bge-m3", "reranker": "dity-ru-msmarco"},
        "bge-m3-hybrid+rerank-bge": {"embedder": "bge-m3", "sparse": True, "reranker": "bge-reranker-v2-m3"},
    }
    # свип RRF: k задаёт вклад лексического канала в слияние
    for k in (10, 30, 100):
        stacks[f"bge-m3-dense+sparse-k{k}"] = {"embedder": "bge-m3", "sparse": True, "rrf_k": k}
    # реранк: топ-2 эмбеддера × 3 кросс-энкодера × глубина пула 20/50/100
    for short, model in (
        ("bge", "bge-reranker-v2-m3"),
        ("jina", "jina-reranker-v2"),
        ("qwen3", "qwen3-reranker-0.6b"),
    ):
        for base in ("bge-m3", "qwen3-0.6b"):
            for depth in (20, 50, 100):
                stacks[f"{base}-dense+rerank-{short}-d{depth}"] = {
                    "embedder": base,
                    "reranker": model,
                    "rerank_depth": depth,
                }
    # Слияние порядка dense и порядка реранкера (RRF) — проверка гипотезы, что
    # кросс-энкодер портит голову выдачи на длинных чанках.
    for base in ("bge-m3", "qwen3-0.6b"):
        for k in (10, 60):
            stacks[f"{base}-dense+rerank-bge-d50-blend{k}"] = {
                "embedder": base,
                "reranker": "bge-reranker-v2-m3",
                "rerank_depth": 50,
                "blend": True,
                "blend_k": k,
            }
    return stacks


if __name__ == "__main__":
    main()
