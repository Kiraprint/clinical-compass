#!/usr/bin/env python3
"""Общие утилиты бенчмарка качества поиска по БАЗЕ ЗНАНИЙ (не по статьям).

Отличие от bench/run_bench.py и bench/eval_app_path.py: корпус — реальный
снимок прод-коллекции Qdrant `kb` (998 чанков: КР ВОЗ, шкалы, препараты,
триаж, статьи), запросы — 150 клинических вопросов (RU первичный + EN),
релевантность — ГРАДУИРОВАННАЯ (0/1/2), размеченная LLM-шлюзом home-stable
с выгрузкой на человеческую проверку.

Данные не в git (bench/data/): корпус снимается с Qdrant (`--export`), запросы
генерируются build_kb_bench.py, qrels — grade_kb_qrels.py.

Запуск метрик: bench/eval_retrieval.py (см. bench/REPORT-retrieval.md).
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
CORPUS = DATA / "kb_corpus.jsonl"
QUERIES = DATA / "kb_queries.jsonl"
QRELS = DATA / "kb_qrels.jsonl"
LLM_DUMP = DATA / "kb_qrels_review.tsv"


def llm_settings() -> tuple[str, str, str]:
    """URL/ключ/модель шлюза: env, иначе — локальный infra/.env (read-only)."""
    url = os.environ.get("CC_ASSISTANT_LLM_URL", "")
    key = os.environ.get("CC_ASSISTANT_LLM_API_KEY", "")
    model = os.environ.get("CC_ASSISTANT_LLM_MODEL", "home-stable")
    # infra/.env лежит в основном чекауте (gitignored); в worktree его может не
    # быть — читаем оттуда же, ничего не записывая (read-only).
    for env_file in (
        HERE.parent / "infra" / ".env",
        HERE.parent.parent.parent / "infra" / ".env",
    ):
        if (url and key) or not env_file.exists():
            continue
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("CC_ASSISTANT_LLM_URL=") and not url:
                url = line.split("=", 1)[1].strip()
            elif line.startswith("CC_ASSISTANT_LLM_API_KEY=") and not key:
                key = line.split("=", 1)[1].strip()
            elif line.startswith("CC_ASSISTANT_LLM_MODEL=") and not os.environ.get("CC_ASSISTANT_LLM_MODEL"):
                model = line.split("=", 1)[1].strip()
    return (url or "http://172.17.0.1:20133/v1").rstrip("/"), key, model

def chat_json(prompt: str, *, system: str = "", timeout: float = 180.0, max_tokens: int = 4096) -> str:
    """Один запрос к OpenAI-совместимому шлюзу, возвращает сырой текст ответа."""
    import urllib.error
    import urllib.request

    url, key, model = llm_settings()
    body = {
        "model": model,
        "messages": (
            ([{"role": "system", "content": system}] if system else [])
            + [{"role": "user", "content": prompt}]
        ),
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        f"{url}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # 4xx/5xx — тело полезно в ошибке
        raise RuntimeError(f"LLM HTTP {exc.code}: {exc.read()[:400]!r}") from exc
    return payload["choices"][0]["message"]["content"]


def parse_json_array(text: str):
    """Достаёт JSON-массив из ответа модели (снимает ```-забор и мусор вокруг).

    Битая разметка (пропущенная запятая, обрезанный хвост) не должна ронять
    многочасовую разметку: пробуем отрезать хвост до последней целой записи,
    иначе возвращаем None — вызывающий делает повторный запрос.
    """
    start = text.find("[")
    if start < 0:
        raise ValueError(f"нет JSON-массива в ответе: {text[:200]!r}")
    end = text.rfind("]")
    body = text[start : end + 1] if end > start else text[start:] + "]"
    try:
        return json.loads(body)
    except ValueError:
        last = body.rfind("},")
        while last > 0:
            try:
                return json.loads(body[: last + 1] + "]")
            except ValueError:
                last = body.rfind("},", 0, last)
        return None


def parse_json_object(text: str):
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"нет JSON-объекта в ответе: {text[:200]!r}")
    return json.loads(text[start : end + 1])


# ---------- корпус ----------
def load_corpus(path: Path = CORPUS) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise SystemExit(f"пустой корпус {path}: запустите build_kb_bench.py")
    return rows


def export_corpus(url: str = "http://127.0.0.1:6333", collection: str = "kb", path: Path = CORPUS) -> int:
    """Снимок прод-коллекции в JSONL: id/guideline_id/source_title/topic/text."""
    from qdrant_client import QdrantClient

    client = QdrantClient(url=url, timeout=30)
    points, _ = client.scroll(
        collection_name=collection, limit=10_000, with_payload=True, with_vectors=False
    )
    rows = []
    for p in sorted(points, key=lambda x: str(x.id)):
        pl = p.payload or {}
        rows.append(
            {
                "id": str(p.id),
                "guideline_id": pl.get("guideline_id"),
                "source_title": pl.get("source_title"),
                "topic": pl.get("topic"),
                "text": pl.get("text") or "",
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows)


# ---------- метрики (градуированные qrels: 0/1/2) ----------
def gains(grades: dict[str, int]) -> dict[str, int]:
    """Вес релевантности 2^g - 1: 2 → 3, 1 → 1, 0 → 0 (стандарт graded nDCG)."""
    return {k: (2 ** g) - 1 for k, g in grades.items()}


def dcg(ranked: list[str], gain: dict[str, int], k: int) -> float:
    return sum(gain.get(d, 0) / math.log2(i + 2) for i, d in enumerate(ranked[:k]))


def ndcg_at(ranked: list[str], grades: dict[str, int], k: int) -> float:
    gain = gains(grades)
    ideal = sorted(gain.values(), reverse=True)[:k]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    if idcg <= 0:
        return 0.0
    return dcg(ranked, gain, k) / idcg


def recall_at(ranked: list[str], grades: dict[str, int], k: int, *, min_grade: int = 1) -> float:
    relevant = {d for d, g in grades.items() if g >= min_grade}
    if not relevant:
        return 0.0
    return len(set(ranked[:k]) & relevant) / len(relevant)


def mrr_at(ranked: list[str], grades: dict[str, int], k: int, *, min_grade: int = 1) -> float:
    for i, d in enumerate(ranked[:k]):
        if grades.get(d, 0) >= min_grade:
            return 1.0 / (i + 1)
    return 0.0


def precision_at(ranked: list[str], grades: dict[str, int], k: int, *, min_grade: int = 1) -> float:
    return sum(1 for d in ranked[:k] if grades.get(d, 0) >= min_grade) / k


def hit_at(ranked: list[str], grades: dict[str, int], k: int, *, min_grade: int = 2) -> float:
    """Строгий hit@k: попал ли в top-k хоть один чанк нужной градации."""
    return 1.0 if any(grades.get(d, 0) >= min_grade for d in ranked[:k]) else 0.0



def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))
    return s[idx]


def evaluate(rankings: dict[str, list[str]], qrels: dict[str, dict[str, int]]) -> dict:
    """Метрики по всем запросам qrels (ранжирования вне qrels игнорируются)."""
    metrics: dict[str, list[float]] = {}

    def add(name: str, value: float) -> None:
        metrics.setdefault(name, []).append(value)

    for qid, grades in qrels.items():
        ranked = rankings.get(qid, [])
        for k in (1, 3, 5, 10):
            add(f"nDCG@{k}", ndcg_at(ranked, grades, k))
            add(f"Recall@{k}", recall_at(ranked, grades, k))
            add(f"Recall2@{k}", recall_at(ranked, grades, k, min_grade=2))
        add("MRR@10", mrr_at(ranked, grades, 10))
        add("Hit3", hit_at(ranked, grades, 3))
        add("Hit5", hit_at(ranked, grades, 5))
        if grades:
            add("judged", float(len(grades)))
    return {name: sum(vals) / len(vals) for name, vals in metrics.items()} | {
        "queries": float(len(qrels))
    }


def evaluate_covered(
    rankings: dict[str, list[str]], qrels: dict[str, dict[str, int]]
) -> dict:
    """Метрики только по запросам, у которых в KB есть ответ (grade=2 в пуле).

    Запросы без grade=2 выпадают из знаменателя (KB не покрывает тему), но их
    доля возвращается как `coverage` — честная шапка отчёта.
    """
    covered = {qid: g for qid, g in qrels.items() if any(v >= 2 for v in g.values())}
    out = evaluate(rankings, covered)
    out["coverage"] = len(covered) / max(1, len(qrels))
    out["uncovered"] = sorted(set(qrels) - set(covered))
    return out



def per_query_report(rankings: dict[str, list[str]], qrels: dict[str, dict[str, int]]) -> list[dict]:
    out = []
    for qid, grades in qrels.items():
        ranked = rankings.get(qid, [])
        out.append(
            {
                "qid": qid,
                "nDCG@3": round(ndcg_at(ranked, grades, 3), 4),
                "Recall@3": round(recall_at(ranked, grades, 3), 4),
                "top3": ranked[:3],
                "top3_grades": [grades.get(d, 0) for d in ranked[:3]],
                "judged": len(grades),
            }
        )
    return out


# ---------- hash-эмбеддер (зеркало app.services.embeddings.HashEmbedder) ----------
# Бенч живёт в отдельном venv без зависимостей API (sqlalchemy и т.п.), поэтому
# baseline hash продублирован здесь. Формула обязана совпадать с прод-кодом:
# токены (слова + 3-граммы) → md5-бакеты → L2-нормировка dense / веса sparse.
import hashlib  # noqa: E402
import re  # noqa: E402

DENSE_SIZE = 512
SPARSE_BUCKETS = 65536


def _tokens(text: str) -> list[str]:
    lower = text.lower()
    words = re.findall(r"[a-zа-яё0-9][a-zа-яё0-9\-]{1,39}", lower)
    tokens = list(words)
    for i in range(len(words) - 2):
        tokens.append(" ".join(words[i : i + 3]))
    return tokens


def _hash_index(token: str, buckets: int) -> int:
    return int(hashlib.md5(token.encode("utf-8")).hexdigest()[:8], 16) % buckets


class HashEmbedderLite:
    dense_size = DENSE_SIZE
    use_sparse = True

    def dense(self, text: str) -> list[float]:
        vec = [0.0] * self.dense_size
        for tok in _tokens(text):
            vec[_hash_index(tok, self.dense_size)] += 1.0
        norm = sum(v * v for v in vec) ** 0.5
        return [v / norm for v in vec] if norm else vec

    def encode_query(self, text: str) -> list[float]:
        return self.dense(text)

    def sparse(self, text: str) -> tuple[list[int], list[float]]:
        weights: dict[int, float] = {}
        for tok in _tokens(text):
            idx = _hash_index(tok, SPARSE_BUCKETS)
            weights[idx] = weights.get(idx, 0.0) + 1.0
        return list(weights), list(weights.values())
