#!/usr/bin/env python3
"""Сводка матрицы: markdown-таблица метрик из сохранённых прогонов eval_retrieval.py.

Пересчитывает метрики по актуальному qrels, так что прогоны без разметки
повторять не нужно. Для каждого стека — метрики по всем размеченным запросам и
по «покрытым» (где в KB есть grade-2), плюс `coverage`.

  python bench/report_table.py bench/data/eval_*.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_bench as kb  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--qrels", default=str(kb.DATA / "qrels.jsonl"))
    ap.add_argument("--json-out", default="")
    ap.add_argument("--sort", default="nDCG@3")
    args = ap.parse_args()

    qrels = {}
    for line in Path(args.qrels).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            qrels[rec["qid"]] = {k: int(v) for k, v in rec["grades"].items()}

    rows = []
    for path in args.files:
        for name, entry in json.loads(Path(path).read_text(encoding="utf-8")).items():
            allm = kb.evaluate(entry["rankings"], qrels)
            cov = kb.evaluate_covered(entry["rankings"], qrels)
            lat = entry.get("latency", {})
            rows.append(
                {
                    "stack": name,
                    "nDCG@3": allm["nDCG@3"],
                    "Hit3": allm["Hit3"],
                    "nDCG@3c": cov["nDCG@3"],
                    "Hit3c": cov["Hit3"],
                    "nDCG@5c": cov["nDCG@5"],
                    "nDCG@10c": cov["nDCG@10"],
                    "R@3c": cov["Recall@3"],
                    "R2@3c": cov["Recall2@3"],
                    "R@10c": cov["Recall@10"],
                    "MRR@10c": cov["MRR@10"],
                    "coverage": cov["coverage"],
                    "n_cov": len(qrels) - len(cov["uncovered"]),
                    "uncovered": cov["uncovered"],
                    "p50_ms": lat.get("p50_ms", 0.0),
                    "p95_ms": lat.get("p95_ms", 0.0),
                    "p99_ms": lat.get("p99_ms", 0.0),
                    "vram_mb": lat.get("vram_mb", 0.0),
                }
            )
    rows.sort(key=lambda r: -r[args.sort])
    cols = ["nDCG@3c", "Hit3c", "nDCG@5c", "nDCG@10c", "R@3c", "R@10c", "R2@3c", "MRR@10c", "nDCG@3", "Hit3"]
    print("| стек | " + " | ".join(cols) + " | coverage | p50, мс | p95, мс | p99, мс | VRAM, МБ |")
    print("|---|" + "---|" * (len(cols) + 5))
    for r in rows:
        cells = " | ".join(f"{r[c]:.3f}" for c in cols)
        print(
            f"| `{r['stack']}` | {cells} | {r['coverage']:.2f} ({r['n_cov']}) | "
            f"{r['p50_ms']:.0f} | {r['p95_ms']:.0f} | {r['p99_ms']:.0f} | {r['vram_mb']:.0f} |"
        )
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
