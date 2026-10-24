"""Слияние пулов кандидатов нескольких стеков в один для LLM-разметки.

Кандидаты объединяются по запросу, порядок — по лучшему рангу среди стеков
(RRF-подобно: сумма 1/(10+rank)), затем обрезка до --cap. Так пул не раздувается
до N×глубины, а в разметку попадают именно «спорные» верхние позиции.

    .venv/bin/python bench/merge_pools.py bench/data/pool600_*.jsonl \
        --cap 30 --out bench/data/kb_pool_600.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(path: Path) -> dict[str, list[str]]:
    pools: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        pools[rec["qid"]] = rec["candidates"]
    return pools


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pools", nargs="+")
    ap.add_argument("--cap", type=int, default=30)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    merged: dict[str, dict[str, float]] = {}
    for path in args.pools:
        for qid, cands in load(Path(path)).items():
            scores = merged.setdefault(qid, {})
            for rank, cid in enumerate(cands):
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (10 + rank)

    total = 0
    out = Path(args.out)
    with out.open("w", encoding="utf-8") as fh:
        for qid in sorted(merged):
            cands = sorted(merged[qid], key=lambda c: -merged[qid][c])[: args.cap]
            total += len(cands)
            fh.write(json.dumps({"qid": qid, "candidates": cands}) + "\n")
    print(f"запросов: {len(merged)}, пар: {total} (cap {args.cap}) → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
