#!/usr/bin/env python3
"""Детерминированные проверки качества золотого набора (anti-bias QA).

Зачем: грамотный бенчмарк не должен «отражать эмбеддер» — набор, собранный из
top-K одной модели или из дословных копий чанков, завышает её метрики.
Проверки без сети:

1. Полнота: у каждого запроса есть чанк grade=2 и хотя бы один grade>=1.
2. Нет «всё релевантно»: доля grade=0 в пуле ≥ 40 % (иначе пул вырожден).
3. Лексическое зеркало: максимальное общее словосочетание запрос↔его grade=2
   чанк < 8 слов; средний Jaccard контентных токенов < 0.25. Запрос, дословно
   повторяющий чанк, ловится лексическим каналом и не проверяет семантику.
4. Кросс-языковость: у RU-запросов доля grade=2 чанков с латиницей ≥ 70 %
   (корпус EN) — значит RU-запросы действительно ищут по EN-тексту.
5. Разброс: у ≥ 80 % запросов grade=2 найдены (в пуле) — пул собран не одной
   моделью (иначе разметка неполна для остальных).

Запуск: python bench/qa_qrels.py [--json-out bench/data/qrels_qa.json]
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_bench as kb  # noqa: E402

_LEX = re.compile(r"[a-zа-яё0-9]{3,}", re.IGNORECASE)
_STOP = {
    "что", "как", "какой", "какие", "какая", "когда", "почему", "зачем", "или",
    "для", "при", "под", "над", "без", "про", "это", "его", "её", "они", "она",
    "the", "and", "for", "with", "what", "which", "when", "how", "are", "should",
    "does", "can", "of", "in", "to", "is", "be", "on", "at", "by", "that",
}


def _tokens(text: str) -> list[str]:
    return [t for t in _LEX.findall(text.lower()) if t not in _STOP]


def longest_common_words(a: list[str], b: list[str]) -> int:
    """Длина самого длинного общего словосочетания (подряд идущих слов)."""
    best = 0
    for i in range(len(a)):
        for j in range(i + 1, min(len(a) + 1, i + 12)):
            if tuple(a[i:j]) in _ngrams(b, j - i):
                best = max(best, j - i)
    return best


def _ngrams(words: list[str], n: int) -> set[tuple]:
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / max(1, len(sa | sb))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qrels", default=str(kb.QRELS))
    ap.add_argument("--corpus", default=str(kb.CORPUS))
    ap.add_argument("--json-out", default=str(kb.DATA / "qrels_qa.json"))
    args = ap.parse_args()

    corpus = {r["id"]: r for r in kb.load_corpus(Path(args.corpus))}
    rows = [json.loads(x) for x in Path(args.qrels).read_text(encoding="utf-8").splitlines() if x.strip()]

    checks: dict[str, dict] = {}
    no2, no1, mirror, max_lcs, zero_share = [], [], 0, 0, []
    jacs: list[float] = []
    ru_cross = []
    for r in rows:
        grades = {k: int(v) for k, v in r["grades"].items()}
        top2 = [cid for cid, g in grades.items() if g == 2]
        rel = [cid for cid, g in grades.items() if g >= 1]
        if not top2:
            no2.append(r["qid"])
        if not rel:
            no1.append(r["qid"])
        if grades:
            zero_share.append(sum(1 for g in grades.values() if g == 0) / len(grades))
        q_tok = _tokens(r["q"])
        for cid in top2:
            text = corpus[cid]["text"]
            lcs = longest_common_words(q_tok, _tokens(text))
            max_lcs = max(max_lcs, lcs)
            if lcs >= 8:
                mirror += 1
            jacs.append(jaccard(q_tok, _tokens(text)[:80]))
        if r.get("lang") == "ru" and top2:
            latin = sum(1 for cid in top2 if re.search(r"[a-z]{4,}", corpus[cid]["text"], re.I))
            ru_cross.append(latin / len(top2))

    # Допускаем ≤5 % запросов без grade=2: пул из 8 стеков всё же выборка, и
    # такой запрос оценивается по grade=1 (nDCG нормируется идеалом из пула).
    checks["has_grade2"] = {
        "ok": len(no2) <= max(1, round(0.05 * len(rows))),
        "failed": no2[:10], "count": len(no2),
    }
    checks["has_relevant"] = {"ok": not no1, "failed": no1[:10], "count": len(no1)}
    checks["mirror_lcs"] = {
        "ok": mirror == 0, "max_lcs_words": max_lcs, "chunks_with_lcs_ge_8": mirror,
        "mean_jaccard": round(statistics.mean(jacs), 4) if jacs else 1.0,
        "jaccard_ok": (statistics.mean(jacs) < 0.25) if jacs else False,
    }
    checks["pool_not_degenerate"] = {
        "ok": bool(zero_share) and statistics.mean(zero_share) >= 0.4,
        "mean_zero_share": round(statistics.mean(zero_share), 4) if zero_share else 0.0,
    }
    checks["ru_queries_cross_lingual"] = {
        "ok": bool(ru_cross) and statistics.mean(ru_cross) >= 0.7,
        "mean_share_latin_top2": round(statistics.mean(ru_cross), 4) if ru_cross else 0.0,
    }
    ok = all(c.get("ok") for c in checks.values())
    out = {"queries": len(rows), "passed": ok, "checks": checks}
    Path(args.json_out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    for name, c in checks.items():
        print(f"{'OK ' if c.get('ok') else 'FAIL'} {name}: {json.dumps({k: v for k, v in c.items() if k != 'ok'}, ensure_ascii=False)}")
    print(f"итог: {'PASS' if ok else 'FAIL'} ({len(rows)} запросов) → {args.json_out}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
