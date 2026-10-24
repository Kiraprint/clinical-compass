#!/usr/bin/env python3
"""Разметка пула кандидатов ЛОКАЛЬНОЙ instruct-моделью на GPU (без шлюза).

Вход:  bench/data/kb_corpus.jsonl, bench/data/kb_queries.jsonl,
       bench/data/kb_pool.jsonl (пул = объединение top-N всех стеков).
Выход: bench/data/qrels.jsonl (+ kb_qrels_review.tsv) — {qid, q, lang, family,
       grades:{chunk_id: 0|1|2}}. Дозапись по одному запросу → прогон прерываем
       и возобновляем без потерь.

Рубрика (та же, что в отчёте):
  2 — фрагмент прямо отвечает на запрос: содержит нужный факт (число пунктов
      шкалы, дозу, критерий, правило red line и т.п.);
  1 — по теме, полезный контекст, но прямого ответа нет;
  0 — не по теме запроса.

Запуск:
  /home/kir/Code/clinical-compass/bench/.venv/bin/python bench/grade_local.py \
      --pool bench/data/kb_pool.jsonl --model Qwen/Qwen3-4B-Instruct-2507
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_bench as kb  # noqa: E402

PROMPT = """Ты — асессор поиска по клинической базе знаний для врача.
Ниже запрос врача и пронумерованные фрагменты базы (документы ВОЗ и карточки препаратов).
Оцени КАЖДЫЙ фрагмент по шкале:
2 — фрагмент прямо отвечает на запрос: в нём есть нужный факт (число пунктов шкалы, доза, критерий, правило, рекомендация);
1 — фрагмент по теме и полезен как контекст, но прямого ответа в нём нет;
0 — фрагмент не по теме запроса.
Язык не важен: английский фрагмент может прямо отвечать на русский вопрос.
Ответь ТОЛЬКО JSON-массивом без пояснений: [{{"id": 1, "grade": 2}}, {{"id": 2, "grade": 0}}, ...]
Каждый номер от 1 до {n} должен встречаться ровно один раз.

Запрос врача: {query}

Фрагменты:
{blocks}
"""


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default=str(kb.DATA / "kb_pool_merged.jsonl"))
    ap.add_argument("--model", default="Qwen/Qwen3-4B-Instruct-2507")
    ap.add_argument("--out", default=str(kb.DATA / "qrels.jsonl"))
    ap.add_argument("--batch", type=int, default=12, help="сколько промптов в одном generate")
    ap.add_argument("--per-prompt", type=int, default=40, help="кандидатов на один промпт")
    ap.add_argument("--chunk-chars", type=int, default=280)
    ap.add_argument("--max-new-tokens", type=int, default=1600)
    ap.add_argument("--limit", type=int, default=0, help="только первые N запросов (отладка)")
    args = ap.parse_args()

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    corpus = {r["id"]: r for r in load_jsonl(kb.CORPUS)}
    queries = {r["id"]: r for r in load_jsonl(kb.QUERIES)}
    pool = load_jsonl(Path(args.pool))
    out_path = Path(args.out)
    done = {r["qid"] for r in load_jsonl(out_path)}
    todo = [p for p in pool if p["qid"] not in done and p["qid"] in queries]
    if args.limit:
        todo = todo[: args.limit]
    print(f"запросов к разметке: {len(todo)} (уже размечено: {len(done)})", flush=True)
    if not todo:
        return

    tok = AutoTokenizer.from_pretrained(args.model, padding_side="left")
    model = AutoModelForCausalLM.from_pretrained(args.model, dtype=torch.bfloat16, device_map="cuda")
    model.eval()

    # каждый запрос → несколько промптов по --per-prompt кандидатов
    jobs: list[tuple[str, list[str]]] = []
    for p in todo:
        cands = p["candidates"]
        for i in range(0, len(cands), args.per_prompt):
            jobs.append((p["qid"], cands[i : i + args.per_prompt]))

    def build(qid: str, cands: list[str]) -> str:
        blocks = "\n\n".join(
            f"[{n + 1}] " + " ".join(corpus[cid]["text"][: args.chunk_chars].split())
            for n, cid in enumerate(cands)
            if cid in corpus
        )
        return PROMPT.format(n=len(cands), query=queries[qid]["q"], blocks=blocks)

    pending: dict[str, dict[str, int]] = {}
    left: dict[str, int] = {}
    for qid, cands in jobs:
        left[qid] = left.get(qid, 0) + len(cands)

    def run_batch(chunk: list[tuple[str, list[str]]]) -> list[dict[int, int]]:
        """Оценки по пачке промптов: [{локальный номер: grade}]."""
        texts_out = []
        for qid, cands in chunk:
            prompt = build(qid, cands)
            texts_out.append(
                tok.apply_chat_template([{"role": "user", "content": prompt}], tokenize=False, add_generation_prompt=True)
            )
        enc = tok(texts_out, return_tensors="pt", padding=True, truncation=True, max_length=8192).to("cuda")
        with torch.inference_mode():
            gen = model.generate(
                **enc,
                max_new_tokens=args.max_new_tokens,
                do_sample=False,
                temperature=None,
                top_p=None,
                pad_token_id=tok.pad_token_id or tok.eos_token_id,
            )
        replies = tok.batch_decode(gen[:, enc["input_ids"].shape[1] :], skip_special_tokens=True)
        chunk_grades = []
        for (qid, cands), reply in zip(chunk, replies):
            local: dict[int, int] = {}
            for item in kb.parse_json_array(reply) or []:
                if not isinstance(item, dict):
                    continue
                try:
                    idx = int(item.get("id", 0)) - 1
                    g = max(0, min(2, int(item.get("grade", 0))))
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(cands) and cands[idx] in corpus:
                    local[idx] = g
            chunk_grades.append(local)
        return chunk_grades

    def merge(chunk: list[tuple[str, list[str]]], chunk_grades: list[dict[int, int]]) -> list[tuple[str, list[str]]]:
        """Сливает оценки в pending; возвращает задания на доспрашивание."""
        retry: list[tuple[str, list[str]]] = []
        for (qid, cands), local in zip(chunk, chunk_grades):
            grades = pending.setdefault(qid, {})
            for idx, g in local.items():
                grades[cands[idx]] = g
            missing = [c for c in cands if c in corpus and c not in grades]
            if missing:
                retry.append((qid, missing))
            elif qid not in done_qids:
                left[qid] -= len(cands)
                if left[qid] <= 0:
                    q = queries[qid]
                    out.write(
                        json.dumps(
                            {"qid": qid, "q": q["q"], "lang": q.get("lang"), "family": q.get("family"), "grades": grades},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    out.flush()
                    done_qids.add(qid)
        return retry

    out = out_path.open("a", encoding="utf-8")
    done_qids: set[str] = set()
    started = time.time()
    retry_jobs: list[tuple[str, list[str]]] = []
    for start in range(0, len(jobs), args.batch):
        chunk = jobs[start : start + args.batch]
        retry_jobs.extend(merge(chunk, run_batch(chunk)))
        if start % (args.batch * 5) == 0 and done_qids:
            el = time.time() - started
            print(f"  готово {len(done_qids)}/{len(todo)} ({el / len(done_qids):.1f} с/запрос)", flush=True)
    # доспрашивание пропущенных номеров (модель иногда теряет часть списка)
    for start in range(0, len(retry_jobs), args.batch):
        chunk = retry_jobs[start : start + args.batch]
        merge(chunk, run_batch(chunk))
    out.close()
    print(f"  размечено запросов: {len(done_qids)} из {len(todo)}", flush=True)

    # человекочитаемая выгрузка для выборочной проверки
    tsv = (kb.DATA / "kb_qrels_review.tsv").open("w", encoding="utf-8")
    tsv.write("qid\tgrade\tchunk_id\tsource_title\tsnippet\tquery\n")
    for rec in load_jsonl(out_path):
        for cid, g in rec["grades"].items():
            c = corpus.get(cid, {})
            snip = " ".join(c.get("text", "")[:160].split()).replace("\t", " ")
            title = (c.get("source_title") or "").replace("\t", " ")
            tsv.write(f"{rec['qid']}\t{g}\t{cid}\t{title}\t{snip}\t{rec['q']}\n")
    tsv.close()
    print(f"готово: {out_path}", flush=True)


if __name__ == "__main__":
    main()
