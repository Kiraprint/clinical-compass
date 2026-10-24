#!/usr/bin/env python3
"""Сводка матрицы: markdown-таблица метрик из сохранённых прогонов eval_retrieval.py.

Читает JSON-ы (--json-out) с полем rankings и пересчитывает метрики по актуальному
файлу qrels — так прогоны без разметки можно не повторять.

  python bench/report_table.py bench/data/eval_A.json bench/data/eval_B.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_bench as kb  # noqa: E402

COLS = ("nDCG@3", "nDCG@5", "nDCG@10", "Recall@1", "Recall@3", "Recall@5", "Recall@10", "Recall2@3", "MRR@10")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--qrels", default=str(kb.QRELS))
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    qrels = {}
    for line in Path(args.qrels).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            qrels[rec["qid"]] = {k: int(v) for k, v in rec["grades"].items()}

    rows = []
    for path in args.files:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for name, entry in data.items():
            metrics = kb.evaluate(entry["rankings"], qrels)
            lat = entry.get("latency", {})
            rows.append(
                {
                    "stack": name,
                    "nDCG@3": metrics["nDCG@3"],
                    "nDCG@5": metrics["nDCG@5"],
                    "nDCG@10": metrics["nDCG@10"],
                    "Recall@1": metrics["Recall@1"],
                    "Recall@3": metrics["Recall@3"],
                    "Recall@5": metrics["Recall@5"],
                    "Recall@10": metrics["Recall@10"],
                    "Recall2@3": metrics["Recall2@3"],
                    "MRR@10": metrics["MRR@10"],
                    "p50_ms": lat.get("p50_ms", 0.0),
                    "p95_ms": lat.get("p95_ms", 0.0),
                    "vram_mb": lat.get("vram_mb", 0.0),
                    "spec": entry.get("spec", {}),
                }
            )
    rows.sort(key=lambda r: -r["nDCG@3"])
    head = "| стек | " + " | ".join(COLS) + " | p50, мс | p95, мс | VRAM, МБ |"
    print(head)
    print("|---|" + "---|" * (len(COLS) + 3))
    for r in rows:
        cells = " | ".join(f"{r[c]:.3f}" for c in COLS)
        print(f"| `{r['stack']}` | {cells} | {r['p50_ms']:.0f} | {r['p95_ms']:.0f} | {r['vram_mb']:.0f} |")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
