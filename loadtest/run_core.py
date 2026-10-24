"""Микробенчмарк ядра: скоринг, триаж, чек-ины, RAG-сборка, эмбеддер.

Скоринг, триаж, чек-ины и деперсонализация — без БД и сети. `build_corpus`
замеряется по TTL-кешу (drugs читаются из БД один раз на сборку снимка).
Дополнительно замеряется реальный путь агента in-process: AgentFactory.invoke →
гибридный поиск Qdrant → build_rag_answer (LLM выключен; нужен провижененный
стенд, иначе замер помечается skipped). Числа: ops/s на одно ядро, p50/p95
в микросекундах, масштабирование по процессам.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from . import common

sys.path.insert(0, str(common.CORE_DIR))

PHQ_ANSWERS = {"q1": 2, "q2": 2, "q3": 1, "q4": 2, "q5": 1, "q6": 1, "q7": 2, "q8": 1, "q9": 0}
GAD_ANSWERS = {"q1": 2, "q2": 2, "q3": 1, "q4": 2, "q5": 1, "q6": 1, "q7": 1}
CHECKIN_ANSWERS = {"MO1": 2, "TR1": 1, "SL1": 2, "IN1": 1, "EN1": 2, "FU1": 1, "HR4": 0}

AGENT_QUERY = "Что говорят исследования о КПТ при генерализованном тревожном расстройстве?"
DOC_TEXT = (
    "Пациент Иванов Иван Иванович, телефон +7 916 123-45-67, email ivanov@example.ru, "
    "паспорт 4510 123456, адрес г. Москва, ул. Тверская, д. 1, кв. 2. "
    "Жалобы: сниженное настроение, нарушение сна, тревога. "
) * 4
RAG_CHUNKS = [
    {
        "text": "КПТ при генерализованном тревожном расстройстве: мета-анализ показал "
        "умеренный эффект по шкале GAD-7 (SMD −0.42).",
        "source_title": "Мета-анализ КПТ при ГАД",
        "guideline_id": "bench-cbt-gad-1",
        "topic": "РЕКОМЕНДАЦИИ",
        "score": 0.91,
    },

    {
        "text": "PHQ-9: порог 10 баллов соответствует умеренной депрессии; "
        "повторная оценка через 2–4 недели.",
        "source_title": "Шкала PHQ-9",
        "guideline_id": "bench-phq9-1",
        "topic": "ШКАЛЫ",
        "score": 0.84,
    },
    {
        "text": "Пациентам с суицидальными мыслями требуется неотложная оценка риска "
        "и маршрутизация к психиатру.",
        "source_title": "Триаж и маршрутизация",
        "guideline_id": "bench-triage-1",
        "topic": "ТРИАЖ",
        "score": 0.79,
    },
]


def _payloads() -> dict:
    from compass.agent import RAGChunk, build_rag_answer, detect_redline
    from compass import checkin as checkin_engine
    from compass.depersonalize import depersonalize
    from compass.routing import routing_for
    from compass.scales import GAD7, PHQ9
    from compass.scoring import risk_flags, score
    from compass.triage import triage

    phq = score(PHQ9, PHQ_ANSWERS)
    phq_flags = risk_flags(PHQ9, PHQ_ANSWERS)
    phq_triage = triage(phq, phq_flags)
    chunks = tuple(RAGChunk(**c) for c in RAG_CHUNKS)

    def pipeline() -> object:
        result = score(PHQ9, PHQ_ANSWERS)
        flags = risk_flags(PHQ9, PHQ_ANSWERS)
        tri = triage(result, flags)
        routing_for(tri, flags, result.severity, delta_7=None)
        return result

    def rag() -> object:
        assert detect_redline("Какой препарат назначить пациенту?") is not None
        assert detect_redline("Что говорят исследования о КПТ при ГАД?") is None
        return build_rag_answer("Что говорят исследования о КПТ при ГАД?", chunks)

    return {
        "scoring.phq9": lambda: score(PHQ9, PHQ_ANSWERS),
        "scoring.gad7": lambda: score(GAD7, GAD_ANSWERS),
        "risk_flags.phq9": lambda: risk_flags(PHQ9, PHQ_ANSWERS),
        "triage.phq9": lambda: triage(phq, phq_flags),
        "routing.phq9": lambda: routing_for(phq_triage, phq_flags, phq.severity, delta_7=None),
        "checkin.plan+planned_items": lambda: (
            checkin_engine.plan(CHECKIN_ANSWERS),
            checkin_engine.planned_items(CHECKIN_ANSWERS),
        ),
        "pipeline.score→flags→triage→routing": pipeline,
        "rag.detect_redline+build_answer": rag,
        "depersonalize.regex_400chars": lambda: depersonalize(DOC_TEXT),
        "embeddings.hash.dense+sparse": _embed,
        "kb_seed.build_corpus (кеш TTL-снимка)": _corpus,
    }


def _corpus() -> object:
    from app.kb_seed import build_corpus

    return build_corpus()


def _embed() -> object:
    from app.services.embeddings import HashEmbedder

    embedder = HashEmbedder()
    text = "КПТ при генерализованном тревожном расстройстве, мета-анализ, GAD-7"
    return embedder.dense(text), embedder.sparse(text)

def measure(fn, n: int, repeats: int = 3) -> dict:
    runs: list[dict] = []
    for _ in range(repeats):
        for _ in range(min(200, n)):
            fn()
        latencies: list[float] = []
        t0 = time.perf_counter()
        for _ in range(n):
            start = time.perf_counter()
            fn()
            latencies.append((time.perf_counter() - start) * 1_000_000)
        wall = time.perf_counter() - t0
        runs.append(
            {
                "ops_per_s": n / wall,
                "us_p50": common.percentile(latencies, 50),
                "us_p95": common.percentile(latencies, 95),
                "us_mean": statistics.fmean(latencies),
            }
        )
    ops = sorted(run["ops_per_s"] for run in runs)
    chosen = runs[[r["ops_per_s"] for r in runs].index(statistics.median(ops))]
    return {
        "n": n,
        "repeats": repeats,
        "ops_per_s": round(chosen["ops_per_s"], 0),
        "ops_per_s_min": round(min(ops), 0),
        "ops_per_s_max": round(max(ops), 0),
        "us_p50": round(chosen["us_p50"], 2),
        "us_p95": round(chosen["us_p95"], 2),
        "us_mean": round(chosen["us_mean"], 2),
    }


def _process_chunk(args: tuple[str, int]) -> tuple[int, float]:
    name, n = args
    payloads = _payloads()
    fn = payloads[name]
    for _ in range(50):
        fn()
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    return n, time.perf_counter() - t0


def scalable(name: str, total_ops: int = 40_000) -> dict:
    workers = [1, 2, 4, 8]
    workers = [w for w in workers if w <= (os.cpu_count() or 1)]
    result: dict = {}
    for w in workers:
        per = max(1, total_ops // w)
        if w == 1:
            _, wall = _process_chunk((name, per))
        else:
            with ProcessPoolExecutor(max_workers=w) as pool:
                walls = [wall for _, wall in pool.map(_process_chunk, [(name, per)] * w)]
            wall = max(walls)
        result[str(w)] = round(w * per / wall, 0)
    return result


def agent_path(n: int = 50, repeats: int = 2) -> dict:
    """Реальный путь агента без HTTP: поиск в Qdrant + сборка ответа (LLM off)."""
    collection = common.resolve_collection()
    try:
        from app.services.agent import AgentFactory
        from qdrant_client import QdrantClient

        client = QdrantClient(url=common.QDRANT_URL)
        if not client.collection_exists(collection):
            return {
                "status": "skipped",
                "reason": f"коллекция {collection!r} не найдена — нужен provision",
            }
    except Exception as exc:  # noqa: BLE001 — замер опционален, ядро — нет
        return {
            "status": "skipped",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    factory = AgentFactory(client)

    def fn() -> dict:
        return factory.invoke(AGENT_QUERY, limit=5)

    fn()  # прогрев: компиляция графа, кеш корпуса, прогрев Qdrant
    return {
        "status": "ok",
        "collection": collection,
        "query": AGENT_QUERY,
        "llm": "disabled",
        **measure(fn, n, repeats),
    }


def run() -> dict:
    # env уже применён точкой входа (main/run_all): повторный apply_env затёр бы
    # --db/--collection/--embedder дефолтами.
    from app import db

    if db.sync_engine is None:
        db.setup(common.db_url())
    sizes = {
        "scoring.phq9": 60_000,
        "scoring.gad7": 60_000,
        "risk_flags.phq9": 60_000,
        "triage.phq9": 20_000,
        "routing.phq9": 20_000,
        "pipeline.score→flags→triage→routing": 20_000,
        "rag.detect_redline+build_answer": 20_000,
        "checkin.plan+planned_items": 60_000,
        "depersonalize.regex_400chars": 10_000,
        "kb_seed.build_corpus (кеш TTL-снимка)": 2_000,
    }
    payloads = _payloads()
    results = {}
    for name, n in sizes.items():
        results[name] = measure(payloads[name], n)
        print(f"{name:45s} {results[name]['ops_per_s']:>10.0f} ops/s  "
              f"p50={results[name]['us_p50']:.1f}µs p95={results[name]['us_p95']:.1f}µs")

    scaling = {
        "scoring.phq9": scalable("scoring.phq9"),
        "pipeline": scalable("pipeline.score→flags→triage→routing"),
        "depersonalize": scalable("depersonalize.regex_400chars", 10_000),
    }
    agent = agent_path()
    if agent["status"] == "ok":
        print(f"agent.AgentFactory.invoke {agent['ops_per_s']:>8.0f} ops/s  "
              f"p50={agent['us_p50']:.1f}µs p95={agent['us_p95']:.1f}µs")
    else:
        print(f"agent.AgentFactory.invoke: {agent['reason']}")
    payload = {
        "cpu_count": os.cpu_count(),
        "functions": results,
        "scaling_ops_per_s_by_processes": scaling,
        "agent_path": agent,
    }
    common.write_json("core.json", payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Микробенчмарк ядра Clinical Compass")
    parser.add_argument("--db", default=None, help="БД (allowlist: compass_load*)")
    parser.add_argument(
        "--collection", default=None, help="коллекция Qdrant (allowlist: kb_load*)"
    )
    parser.add_argument("--embedder", default="hash", choices=["hash", "bge-m3"])
    args = parser.parse_args(argv)
    try:
        common.apply_env(
            embedder=args.embedder, collection=args.collection, db=args.db
        )
    except common.SafetyRefusal as exc:
        print(f"ОТКАЗ: {exc}", file=sys.stderr)
        return common.SafetyRefusal.exit_code
    print(json.dumps(run(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
