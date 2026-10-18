"""Бенч извлечения из клинических историй (метрика кейса 2).

Запуск из корня репо:
    .venv/bin/python bench/history/run.py

Считает по каждому кейсу: полноту/точность кодов МКБ-10 и препаратов, наличие
разделов (жалобы/анамнез/баллы шкал). Итог — средние по датасету.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "api"))

from app.services.history_extract import extract_history  # noqa: E402

DATA = Path(__file__).parent / "dataset.json"


def _prf(gold: list[str], got: list[str]) -> tuple[float, float, float]:
    """precision/recall/F1 по множествам (подстроки для препаратов)."""
    if not gold and not got:
        return 1.0, 1.0, 1.0
    hit = sum(1 for g in gold if any(g.lower() in x.lower() for x in got))
    p = hit / len(got) if got else 0.0
    r = hit / len(gold) if gold else 0.0
    f1 = (2 * p * r / (p + r)) if (p + r) else 0.0
    return p, r, f1


def main() -> None:
    cases = json.loads(DATA.read_text(encoding="utf-8"))["cases"]
    rows = []
    for c in cases:
        ext = extract_history(c["text"], c["id"])
        gold = c["gold"]
        dp, dr, df1 = _prf(gold["diagnosis_codes"], list(ext.diagnosis_codes))
        tp, tr, tf1 = _prf(gold["drugs"], list(ext.therapy))
        presence = [
            (gold["has_complaints"] == bool(ext.complaints)),
            (gold["has_anamnesis"] == bool(ext.anamnesis)),
            (gold["has_scales"] == bool(ext.scale_scores)),
        ]
        rows.append((c["id"], df1, tf1, sum(presence) / 3))

    print(f"{'case':<16}{'dx F1':>8}{'drug F1':>9}{'presence':>10}")
    for cid, df1, tf1, pres in rows:
        print(f"{cid:<16}{df1:>8.2f}{tf1:>9.2f}{pres:>10.2f}")
    n = len(rows)
    print("-" * 43)
    print(f"{'MEAN':<16}{sum(r[1] for r in rows)/n:>8.2f}{sum(r[2] for r in rows)/n:>9.2f}{sum(r[3] for r in rows)/n:>10.2f}")


if __name__ == "__main__":
    main()
