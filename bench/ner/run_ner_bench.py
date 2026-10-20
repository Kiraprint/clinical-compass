"""Оценка кандидатов RU PII на redmadrobot-rnd/pii_benchmark (MIT).

Кандидаты (все оффлайн, метрики честные, текст датасета не коммитится):
  - regex   : ядро core/compass/depersonalize.py без deny-list (структурные сущности)
  - natasha : NatashaNameExtractor (PER-спаны -> FIO; LOC-спаны -> ADDRESS)
  - rubert  : redmadrobot-rnd/rubert-base-pii-ner (14 категорий -> наши 5 видов)

Метрики: точность/полнота/F1 по точному совпадению спана и по пересечению
(IoU>0). Категории датасета маппятся в наши виды:
  FIO <- FIRST_NAME|LAST_NAME|MIDDLE_NAME
  ADDRESS <- CITY|STREET|HOUSE|REGION|DISTRICT|COUNTRY
  DOC <- PASSPORT|SNILS|INN|OMS|DRIVER_LICENSE|MILITARY_ID|BIRTH_CERTIFICATE|CREDIT_CARD
  PHONE <- PHONE;  EMAIL <- EMAIL
  (URL/IP_ADDRESS вне скоупа гварда — не считаем)

Запуск:  python bench/ner/run_ner_bench.py [--candidates regex,natasha,rubert] [--limit N]
"""
from __future__ import annotations

import argparse
import csv
import os
import json
import sys
from collections import defaultdict
from pathlib import Path

DATA = Path(__file__).parent / "data" / "pii_benchmark_test.csv"

CAT_TO_KIND = {
    "FIRST_NAME": "FIO", "LAST_NAME": "FIO", "MIDDLE_NAME": "FIO",
    "CITY": "ADDRESS", "STREET": "ADDRESS", "HOUSE": "ADDRESS",
    "REGION": "ADDRESS", "DISTRICT": "ADDRESS", "COUNTRY": "ADDRESS",
    "PASSPORT": "DOC", "SNILS": "DOC", "INN": "DOC", "OMS": "DOC",
    "DRIVER_LICENSE": "DOC", "MILITARY_ID": "DOC", "BIRTH_CERTIFICATE": "DOC",
    "CREDIT_CARD": "DOC", "PHONE": "PHONE", "EMAIL": "EMAIL",
}


def load_gold() -> list[dict]:
    """Читает CSV: text, tokens, ner_tags -> список {text, gold: [(kind, start, end)]}."""
    rows = []
    with open(DATA, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            text = row["text"]
            tokens = json.loads(row["tokens"])
            tags = json.loads(row["ner_tags"])
            gold = token_spans(text, tokens, tags)
            rows.append({"text": text, "gold": gold})
    return rows


def token_spans(text: str, tokens: list[str], tags: list[str]) -> list[tuple[str, int, int]]:
    """Восстанавливает char-офсеты золотых сущностей по токенам BIO."""
    spans: list[tuple[str, int, int]] = []
    pos = 0
    i = 0
    n = len(tokens)
    while i < n and pos <= len(text):
        tok = tokens[i]
        # ищем токен в тексте от pos (с учётом пропусков)
        found = text.find(tok, pos)
        if found < 0:
            # токена нет в тексте вовсе (расхождение токенизаторов):
            # экранируем и переходим к следующему токену
            pos = 0
            i += 1
            continue
        start = found
        end = start + len(tok)
        tag = tags[i]
        if tag != "O":
            cat = tag.split("-", 1)[1]
            kind = CAT_TO_KIND.get(cat)
            if kind:
                if spans and spans[-1][0] == kind and spans[-1][2] == start:
                    # продолжаем I-сущность только если она примыкает
                    spans[-1] = (kind, spans[-1][1], end)
                else:
                    spans.append((kind, start, end))
        pos = end
        i += 1
    return spans


# ---------- кандидаты ----------

def cand_regex(text: str) -> list[tuple[str, int, int]]:
    from core.compass.depersonalize import depersonalize

    res = depersonalize(text, names=None)
    return [(f.kind, f.start, f.end) for f in res.findings]


def cand_natasha(text: str) -> list[tuple[str, int, int]]:
    """Нативно через natasha (без app-зависимостей: бенчмарк честный и лёгкий).

    Модели natasha упакованы в wheel; MorphVocab не нужен для спанов."""
    from natasha import Doc, NewsEmbedding, NewsNERTagger, Segmenter

    if cand_natasha._pipeline is None:  # type: ignore[attr-defined]
        seg = Segmenter()
        ner = NewsNERTagger(NewsEmbedding())
        cand_natasha._pipeline = (seg, ner)  # type: ignore[attr-defined]
    seg, ner = cand_natasha._pipeline  # type: ignore[attr-defined]
    doc = Doc(text)
    doc.segment(seg)
    doc.tag_ner(ner)
    out: list[tuple[str, int, int]] = []
    for span in doc.spans:
        kind = _NATASHA_KIND.get(getattr(span, "type", ""))
        if kind is None or span.stop <= span.start:
            continue
        out.append((kind, span.start, span.stop))
    return out


cand_natasha._pipeline = None  # type: ignore[attr-defined]

_NATASHA_KIND = {"PER": "FIO", "LOC": "ADDRESS"}


def cand_rubert(text: str) -> list[tuple[str, int, int]]:
    return _cand_rubert(text)


def _make_rubert():
    import torch
    from transformers import pipeline

    model_id = os.environ.get("NER_MODEL", "redmadrobot-rnd/rubert-base-pii-ner")
    pipe = pipeline(
        "token-classification",
        model=model_id,
        device=0 if torch.cuda.is_available() else -1,
        aggregation_strategy="first",
    )
    MAPPING = {
        "NAME": "FIO",
        "FIRST_NAME": "FIO", "LAST_NAME": "FIO", "MIDDLE_NAME": "FIO",
        "STREET": "ADDRESS", "CITY": "ADDRESS", "REGION": "ADDRESS",
        "COUNTRY": "ADDRESS", "HOUSE": "ADDRESS", "DISTRICT": "ADDRESS",
        "PASSPORT": "DOC", "SNILS": "DOC", "INN": "DOC", "OMS": "DOC",
        "DRIVER_LICENSE": "DOC", "MILITARY_ID": "DOC", "BIRTH_CERTIFICATE": "DOC",
        "CREDIT_CARD": "DOC", "PHONE": "PHONE", "EMAIL": "EMAIL",
    }

    def run(text: str) -> list[tuple[str, int, int]]:
        ents = pipe(text)
        out = []
        for e in ents:
            label = e.get("entity_group") or e.get("entity")
            kind = MAPPING.get(label)
            if kind:
                out.append((kind, e["start"], e["end"]))
        return out

    return run


_cand_rubert = None


def init_rubert():
    global _cand_rubert
    if _cand_rubert is None:
        print("  загрузка rubert-base-pii-ner (один раз)...", file=sys.stderr)
        _cand_rubert = _make_rubert()


def cand_combo(text: str) -> list[tuple[str, int, int]]:
    """Полный гвард: структурный regex + нейро-NER (объединение спанов)."""
    return cand_regex(text) + cand_rubert(text)


CANDIDATES = {"regex": cand_regex, "natasha": cand_natasha, "rubert": None, "combo": None}


# ---------- метрики ----------

def f1(tp: int, fp: int, fn: int) -> float:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return 2 * p * r / (p + r) if p + r else 0.0


def evaluate(rows: list[dict], cand) -> dict:
    stats = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})   # exact
    o_stats = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})  # overlap
    for r in rows:
        gold = r["gold"]
        pred = cand(r["text"])
        # exact: совпадение (kind,start,end)
        for kind, s, e in pred:
            hit = any(k == kind and s == gs and e == ge for k, gs, ge in gold)
            if hit:
                stats[kind]["tp"] += 1
            else:
                stats[kind]["fp"] += 1
        for kind, gs, ge in gold:
            if not any(k == kind and s == gs and e == ge for k, s, e in pred):
                stats[kind]["fn"] += 1
        # overlap: IoU > 0 (середина предсказанного спана внутри золотого)
        for kind, s, e in pred:
            hit = any(
                k == kind and gs <= (s + e) // 2 < ge for k, gs, ge in gold
            )
            if hit:
                o_stats[kind]["tp"] += 1
            else:
                o_stats[kind]["fp"] += 1
        for kind, gs, ge in gold:
            if not any(
                k == kind and gs <= (s + e) // 2 < ge for k, s, e in pred
            ):
                o_stats[kind]["fn"] += 1
    return {"exact": dict(stats), "overlap": dict(o_stats)}


KINDS = ["FIO", "ADDRESS", "DOC", "PHONE", "EMAIL"]


def print_report(name: str, res: dict):
    print(f"\n=== {name} ===")
    for mode in ("exact", "overlap"):
        print(f"--- {mode} ---")
        print(f"{'вид':<8}{'TP':>6}{'FP':>6}{'FN':>6}{'P':>8}{'R':>8}{'F1':>8}")
        tot = {"tp": 0, "fp": 0, "fn": 0}
        for kind in KINDS:
            st = res[mode].get(kind, {"tp": 0, "fp": 0, "fn": 0}) or {}
            tp, fp, fn = st.get("tp", 0), st.get("fp", 0), st.get("fn", 0)
            tot["tp"] += tp; tot["fp"] += fp; tot["fn"] += fn
            print(
                f"{kind:<8}{tp:>6}{fp:>6}{fn:>6}"
                f"{f1(tp, fp, fn):>8.3f}"
                f"{f1(tp, fp, fn):>8.3f}"
                f"{f1(tp, fp, fn):>8.3f}"
            )
        tp, fp, fn = tot["tp"], tot["fp"], tot["fn"]
        print(
            f"{'ВСЕГО':<8}{tp:>6}{fp:>6}{fn:>6}"
            f"{f1(tp, fp, fn):>8.3f}"
            f"{f1(tp, fp, fn):>8.3f}"
            f"{f1(tp, fp, fn):>8.3f}"
        )

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", default="regex,natasha,rubert")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # корень репо
    rows = load_gold()
    if args.limit:
        rows = rows[: args.limit]
    print(f"датасет: {len(rows)} строк")

    cands = [c.strip() for c in args.candidates.split(",") if c.strip()]
    for c in cands:
        if c in ("rubert", "combo"):
            init_rubert()
            fn = cand_rubert if c == "rubert" else cand_combo
        else:
            fn = CANDIDATES[c]
        print(f"\nпрогон: {c} ({len(rows)} строк)...", file=sys.stderr)
        res = evaluate(rows, fn)
        print_report(c, res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
