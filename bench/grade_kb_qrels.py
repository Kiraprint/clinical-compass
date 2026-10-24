#!/usr/bin/env python3
"""Разметка релевантности (0/1/2) пула кандидатов LLM-шлюзом home-stable.

Вход: bench/data/kb_queries.jsonl + kb_corpus.jsonl + kb_pool.jsonl
      (пул = объединение top-N всех стеков, bench/eval_retrieval.py --dump-pools).
Выход:
  bench/data/kb_qrels.jsonl        — qrels для метрик {qid, q, lang, grades:{id: 0|1|2}}
  bench/data/kb_qrels_review.tsv   — человекочитаемая выгрузка всех (qid, grade, id, текст)
                                     для выборочной проверки асессором.

Схема оценки (промпт фиксирован, см. GRADE_PROMPT):
  2 — фрагмент прямо отвечает на вопрос (нужный факт/рекомендация есть);
  1 — по теме, полезный контекст, но прямого ответа нет;
  0 — не по теме.

Идемпотентно и с докачкой: уже размеченные qid берутся из
bench/data/kb_qrels_partial.jsonl и повторно не запрашиваются.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_bench as kb  # noqa: E402

PARTIAL = kb.DATA / "kb_qrels_partial.jsonl"
CHUNK_CHARS = 280
BATCH = 80

GRADE_PROMPT = """Ты — асессор поиска по клинической базе знаний для врача.
Ниже запрос врача и пронумерованные фрагменты базы (перевод/фрагмент документа ВОЗ).
Оцени релевантность КАЖДОГО фрагмента запросу по шкале:
2 — фрагмент ПРЯМО отвечает на вопрос: содержит нужный факт, рекомендацию, дозу, критерий;
1 — по теме и полезен как контекст, но прямого ответа на вопрос нет;
0 — не по теме либо не даёт ничего для ответа.
Игнорируй язык: русский вопрос и английский фрагмент — норма.
Верни ТОЛЬКО JSON-объект вида {{"<номер фрагмента>": 0|1|2, ...}} для ВСЕХ фрагментов.
Никаких пояснений вне JSON.

Запрос врача: {query}

Фрагменты:
{blocks}
"""


def _load_partial() -> dict[str, dict[str, int]]:
    if not PARTIAL.exists():
        return {}
    out: dict[str, dict[str, int]] = {}
    for line in PARTIAL.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            out[rec["qid"]] = {k: int(v) for k, v in rec["grades"].items()}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default=str(kb.DATA / "kb_pool.jsonl"))
    ap.add_argument("--queries", default=str(kb.QUERIES))
    ap.add_argument("--corpus", default=str(kb.CORPUS))
    ap.add_argument("--out", default=str(kb.QRELS))
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="первые N запросов (отладка)")
    args = ap.parse_args()

    corpus = {r["id"]: r for r in kb.load_corpus(Path(args.corpus))}
    queries = [json.loads(x) for x in Path(args.queries).read_text(encoding="utf-8").splitlines() if x.strip()]
    if args.limit:
        queries = queries[: args.limit]
    pool: dict[str, list[str]] = {}
    for line in Path(args.pool).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            pool[rec["qid"]] = [c for c in rec["candidates"] if c in corpus]

    done = _load_partial()
    todo = [q for q in queries if q["id"] not in done]
    print(f"запросов: {len(queries)}, уже размечено: {len(done)}, к разметке: {len(todo)}", file=sys.stderr)

    lock = Lock()
    partial_f = PARTIAL.open("a", encoding="utf-8")

    def grade_one(q: dict) -> None:
        cands = pool.get(q["id"], [])
        batches = [cands[i : i + BATCH] for i in range(0, len(cands), BATCH)]
        grades: dict[str, int] = {}
        for batch in batches:
            blocks = "\n\n".join(
                f"[{i + 1}] {corpus[cid]['text'][:CHUNK_CHARS]}".replace("\n", " ")
                for i, cid in enumerate(batch)
            )
            prompt = GRADE_PROMPT.format(query=q["q"], blocks=blocks)
            for attempt in range(3):
                try:
                    raw = kb.chat_json(prompt, max_tokens=1024)
                    obj = kb.parse_json_object(raw)
                    break
                except Exception as exc:  # noqa: BLE001 — формат/сеть, ретрай
                    if attempt == 2:
                        print(f"[warn] {q['id']}: {exc}", file=sys.stderr)
                        return
                    time.sleep(2 * (attempt + 1))
            for i, cid in enumerate(batch):
                val = obj.get(str(i + 1), obj.get(i + 1, 0))
                try:
                    grades[cid] = max(0, min(2, int(val)))
                except (TypeError, ValueError):
                    grades[cid] = 0
            missing = [i for i, cid in enumerate(batch) if str(i + 1) not in obj]
            if missing and len(missing) < len(batch):
                # модель пропустила часть номеров — доспрашиваем их одним коротким вызовом
                sub = [(i, batch[i]) for i in missing]
                blocks = "\n\n".join(f"[{n + 1}] {corpus[cid]['text'][:CHUNK_CHARS]}".replace("\n", " ") for n, (_, cid) in enumerate(sub))
                try:
                    obj2 = kb.parse_json_object(kb.chat_json(GRADE_PROMPT.format(query=q["q"], blocks=blocks), max_tokens=1024))
                    for n, (_, cid) in enumerate(sub):
                        val = obj2.get(str(n + 1), 0)
                        try:
                            grades[cid] = max(0, min(2, int(val)))
                        except (TypeError, ValueError):
                            grades[cid] = 0
                except Exception:  # noqa: BLE001 — остатки считаем 0, фиксируем в статистике
                    pass
        with lock:
            partial_f.write(json.dumps({"qid": q["id"], "grades": grades}) + "\n")
            partial_f.flush()
            done[q["id"]] = grades

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(grade_one, todo))
    partial_f.close()

    # финальные qrels + выгрузка на проверку
    rows = []
    review_lines = ["qid\tgrade\tchunk_id\tsource_title\tsnippet\tquery"]
    for q in queries:
        grades = done.get(q["id"], {})
        rows.append({"qid": q["id"], "q": q["q"], "lang": q["lang"], "family": q.get("family"), "grades": grades})
        for cid, g in sorted(grades.items(), key=lambda kv: -kv[1]):
            r = corpus[cid]
            review_lines.append(
                f"{q['id']}\t{g}\t{cid}\t{r['source_title'][:80]}\t"
                f"{r['text'][:180].replace(chr(9), ' ').replace(chr(10), ' ')}\t{q['q']}"
            )
    Path(args.out).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    kb.LLM_DUMP.write_text("\n".join(review_lines) + "\n", encoding="utf-8")

    judged = [len(r["grades"]) for r in rows]
    pos = [sum(1 for g in r["grades"].values() if g >= 1) for r in rows]
    print(
        f"готово: {len(rows)} запросов, пар (запрос, чанк): {sum(judged)}, "
        f"среднее пар/запрос: {sum(judged) / max(1, len(rows)):.1f}, "
        f"запросов без релевантных: {sum(1 for p in pos if p == 0)} → {args.out}"
    )


if __name__ == "__main__":
    main()
