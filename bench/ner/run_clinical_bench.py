"""Бенчмарк деперсонализации на клиническом gold-корпусе (docs/12).

Корпус: bench/ner/clinical_contexts/corpus.jsonl (синтетика, gold по
конструкции). Кандидаты:
  regex   — только структурные правила ядра (core/compass/depersonalize.py)
  rubert  — redmadrobot-rnd/rubert-base-pii-ner (без структурных правил)
  gliner  — urchade/gliner_multi_pii-v1 (zero-shot PII, без правил)
  llm     — извлечение сущностей через OpenAI-совместимый шлюз (home-stable)
  hybrid  — структурные правила + rubert + валидаторы (прод-путь)

Метрики: entity-level precision/recall по категориям. Совпадение — пересечение
≥ 50% длины более короткого спана (маскирование посимвольное, поэтому
частичный охват адреса считается попаданием); дополнительно пишется exact.

Запуск (in-process кандидаты):
  bench/.venv/bin/python bench/ner/run_clinical_bench.py --predict regex,rubert,hybrid
GLiNER (своё окружение: gliner + transformers<5.17):
  /tmp/pii-gliner/bin/python bench/ner/run_clinical_bench.py --predict gliner --save
LLM-кандидат (нужен шлюз из infra/.env):
  bench/.venv/bin/python bench/ner/run_clinical_bench.py --predict llm --save
Отчёт по сохранённым предсказаниям:
  bench/.venv/bin/python bench/ner/run_clinical_bench.py --eval-only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "core"), str(ROOT / "api")]

CORPUS = Path(__file__).parent / "clinical_contexts" / "corpus.jsonl"
PRED_DIR = Path(__file__).parent / "clinical_contexts" / "preds"
CATEGORIES = ("FIO", "PHONE", "EMAIL", "ADDRESS", "DOB", "SNILS", "INN",
              "OMS", "PASSPORT", "MEDCARD", "AGE", "OTHER")
SCOPE_NOTE = ("AGE — комбо с ФИО; одиночный возраст не ПДн и в gold не входит")

CAT_OF_KIND = {}
for _k in ("FIO", "PHONE", "EMAIL", "ADDRESS", "DOB", "AGE"):
    CAT_OF_KIND[_k] = _k


def load_corpus() -> list[dict]:
    return [json.loads(line) for line in CORPUS.read_text(encoding="utf-8").splitlines() if line]


def _cat(kind: str, category: str | None) -> str:
    """Категория предсказания; DOC без уточнения — OTHER (считается FP)."""
    if category or kind in CAT_OF_KIND:
        return category or kind
    return "OTHER"


def _span_dicts(spans) -> list[dict]:
    return [{"kind": s.kind, "category": _cat(s.kind, s.category),
             "start": s.start, "end": s.end} for s in spans]


# ------------------------------------------------------------ кандидаты

def cand_regex(text: str) -> list[dict]:
    from compass.depersonalize import structural_spans, resolve_spans

    # resolve без ФИО: возраст-комбо не подтверждается, поэтому AGE отсеется.
    return _span_dicts(resolve_spans(structural_spans(text), text))


_rubert_pipe = None


def _rubert_pipe_init():
    global _rubert_pipe
    if _rubert_pipe is not None:
        return _rubert_pipe
    import torch
    from transformers import pipeline

    model = os.environ.get("NER_MODEL", "redmadrobot-rnd/rubert-base-pii-ner")
    _rubert_pipe = pipeline(
        "token-classification", model=model,
        device=0 if torch.cuda.is_available() else -1,
        aggregation_strategy="first",
    )
    return _rubert_pipe


_RUBERT_MAP = {
    "NAME": ("FIO", None), "FIRST_NAME": ("FIO", None),
    "LAST_NAME": ("FIO", None), "MIDDLE_NAME": ("FIO", None),
    "STREET": ("ADDRESS", None), "CITY": ("ADDRESS", None),
    "REGION": ("ADDRESS", None), "COUNTRY": ("ADDRESS", None),
    "HOUSE": ("ADDRESS", None), "DISTRICT": ("ADDRESS", None),
    "PHONE": ("PHONE", None), "EMAIL": ("EMAIL", None),
    "PASSPORT": ("DOC", "PASSPORT"), "SNILS": ("DOC", "SNILS"),
    "INN": ("DOC", "INN"), "OMS": ("DOC", "OMS"),
    "DRIVER_LICENSE": ("DOC", None), "MILITARY_ID": ("DOC", None),
    "BIRTH_CERTIFICATE": ("DOC", None), "CREDIT_CARD": ("DOC", None),
}


def cand_rubert(text: str) -> list[dict]:
    out = []
    for e in _rubert_pipe_init()(text):
        label = e.get("entity_group") or e.get("entity")
        mapped = _RUBERT_MAP.get(label)
        if not mapped or int(e["end"]) <= int(e["start"]):
            continue
        kind, cat = mapped
        out.append({"kind": kind, "category": cat or (kind if kind in CAT_OF_KIND else "OTHER"),
                    "start": int(e["start"]), "end": int(e["end"])})
    return out


def cand_hybrid(text: str) -> list[dict]:
    from compass.depersonalize import (
        Span, extract_spans, is_plausible_address_span, is_plausible_person_span,
    )

    extra = []
    for s in cand_rubert(text):
        if s["kind"] not in ("FIO", "ADDRESS"):
            continue
        span = Span(s["kind"], s["start"], s["end"])
        if is_plausible_person_span(text, span) and is_plausible_address_span(
                text, span):
            extra.append((s["kind"], s["start"], s["end"]))
    return _span_dicts(extract_spans(text, extra_spans=extra))


GLINER_LABELS = [
    "ФИО", "адрес", "номер телефона", "email", "паспорт", "СНИЛС", "ИНН",
    "полис ОМС", "дата рождения", "номер медицинской карты",
]
_GLINER_MAP = {
    "ФИО": ("FIO", None), "адрес": ("ADDRESS", None),
    "номер телефона": ("PHONE", None), "email": ("EMAIL", None),
    "паспорт": ("DOC", "PASSPORT"), "СНИЛС": ("DOC", "SNILS"),
    "ИНН": ("DOC", "INN"), "полис ОМС": ("DOC", "OMS"),
    "дата рождения": ("DOB", None), "номер медицинской карты": ("DOC", "MEDCARD"),
}
_gliner_model = None


def cand_gliner(text: str, threshold: float = 0.3) -> list[dict]:
    global _gliner_model
    if _gliner_model is None:
        from gliner import GLiNER

        _gliner_model = GLiNER.from_pretrained(
            os.environ.get("GLINER_MODEL", "urchade/gliner_multi_pii-v1"))
        _gliner_model.eval()
    out = []
    for e in _gliner_model.predict_entities(text, GLINER_LABELS, threshold=threshold):
        mapped = _GLINER_MAP.get(e["label"])
        if not mapped:
            continue
        kind, cat = mapped
        out.append({"kind": kind, "category": cat or (kind if kind in CAT_OF_KIND else "OTHER"),
                    "start": int(e["start"]), "end": int(e["end"])})
    return out


# ------------------------------------------------------------ LLM

_LLM_PROMPT = """Ты — деперсонализатор медицинского текста. Найди в каждом
фрагменте персональные данные и верни СТРОГО JSON-массив объектов:
{"id": <id фрагмента>, "entities": [{"category": "<категория>", "text": "<точная подстрока>"}]}

Категории: FIO (ФИО), PHONE, EMAIL, ADDRESS (адрес), DOB (дата рождения —
только при маркере «дата рождения/д.р./родился/г.р.»), SNILS, INN, OMS (полис
ОМС), PASSPORT, MEDCARD (номер медкарты/ИБ), AGE (возраст — только если рядом
в том же предложении есть ФИО).

Правила: text — дословная подстрока фрагмента, без маркеров («дата рождения»
не включай, только дату); даты приёма/симптомов без маркера рождения НЕ
размечай; одиночный возраст без ФИО НЕ размечай; ничего не придумывай, если
сущностей нет — "entities": []. Ответ — только JSON, без пояснений.

Фрагменты:
"""


def _llm_call(fragments: list[dict]) -> tuple[list[dict], float, dict]:
    import urllib.request

    url = os.environ.get("CC_ASSISTANT_LLM_URL", "http://172.17.0.1:20133/v1").rstrip("/")
    key = os.environ.get("CC_ASSISTANT_LLM_API_KEY", "")
    payload = {
        "model": os.environ.get("CC_CLINICAL_LLM_MODEL", "home-stable"),
        "temperature": 0,
        "messages": [
            {"role": "system", "content": _LLM_PROMPT},
            {"role": "user", "content": json.dumps(fragments, ensure_ascii=False)},
        ],
    }
    req = urllib.request.Request(
        url + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {key}"} if key else {})},
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read())
    elapsed = time.monotonic() - t0
    content = data["choices"][0]["message"].get("content") or ""
    content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.M).strip()
    m = re.search(r"\[.*\]", content, re.S)
    parsed = json.loads(m.group(0)) if m else []
    return parsed, elapsed, data.get("usage") or {}


def cand_llm_all(corpus: list[dict], batch: int) -> tuple[dict[str, list[dict]], dict]:
    """Батчевый LLM-кандидат: возвращает предсказания и метрики вызовов."""
    preds: dict[str, list[dict]] = {}
    stats = {"calls": 0, "seconds": 0.0, "prompt_tokens": 0,
             "completion_tokens": 0, "per_context_seconds": []}
    for i in range(0, len(corpus), batch):
        chunk = corpus[i:i + batch]
        fragments = [{"id": r["id"], "text": r["text"]} for r in chunk]
        try:
            parsed, elapsed, usage = _llm_call(fragments)
        except Exception as exc:  # noqa: BLE001 — сеть/формат: кандидат честно падает
            print(f"  LLM batch {i // batch}: {type(exc).__name__}: {exc}", file=sys.stderr)
            for r in chunk:
                preds[r["id"]] = []
            continue
        stats["calls"] += 1
        stats["seconds"] += elapsed
        stats["per_context_seconds"].append(elapsed / len(chunk))
        stats["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        stats["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        by_id = {str(p.get("id")): p.get("entities") or [] for p in parsed}
        for r in chunk:
            spans, cursor = [], 0
            for ent in by_id.get(r["id"], []):
                value = (ent.get("text") or "").strip()
                category = str(ent.get("category") or "").upper()
                if not value or category not in CATEGORIES:
                    continue
                pos = r["text"].find(value, cursor)
                if pos < 0:
                    pos = r["text"].find(value)
                if pos < 0:
                    continue  # модель переписала подстроку — не засчитываем
                kind = "DOC" if category in ("SNILS", "INN", "OMS", "PASSPORT",
                                             "MEDCARD") else category
                spans.append({"kind": kind, "category": category,
                              "start": pos, "end": pos + len(value)})
                cursor = pos + len(value)
            preds[r["id"]] = spans
    return preds, stats


# ------------------------------------------------------------ метрики

def evaluate(gold_rows: list[dict], preds: dict[str, list[dict]]) -> dict:
    per_cat = {c: {"tp": 0, "fp": 0, "fn": 0, "exact": 0} for c in CATEGORIES}
    misses, fps = [], []
    for row in gold_rows:
        gold = sorted(row["gold"], key=lambda g: g["start"])
        pred = sorted(preds.get(row["id"], []), key=lambda p: p["start"])
        used = [False] * len(pred)
        for g in gold:
            cat = g["kind"]
            best = None
            for i, p in enumerate(pred):
                if used[i] or p["category"] != cat:
                    continue
                inter = min(g["end"], p["end"]) - max(g["start"], p["start"])
                if inter <= 0:
                    continue
                shorter = min(g["end"] - g["start"], p["end"] - p["start"])
                if inter >= 0.5 * shorter:
                    best = (i, inter)
                    break
            if best is None:
                per_cat[cat]["fn"] += 1
                if len(misses) < 40:
                    misses.append((row["id"], cat, g["value"], row["text"]))
            else:
                idx, _ = best
                used[idx] = True
                per_cat[cat]["tp"] += 1
                p = pred[idx]
                if p["start"] == g["start"] and p["end"] == g["end"]:
                    per_cat[cat]["exact"] += 1
        for i, p in enumerate(pred):
            if not used[i]:
                per_cat[p["category"]]["fp"] += 1
                if len(fps) < 40:
                    fps.append((row["id"], p, row["text"][p["start"]:p["end"]]))

    def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
        p = tp / (tp + fp) if tp + fp else 1.0
        r = tp / (tp + fn) if tp + fn else 1.0
        f = 2 * p * r / (p + r) if p + r else 0.0
        return p, r, f

    rows = []
    for cat in CATEGORIES:
        d = per_cat[cat]
        p, r, f = prf(d["tp"], d["fp"], d["fn"])
        rows.append({"category": cat, **d, "precision": p, "recall": r, "f1": f})
    tp = sum(d["tp"] for d in per_cat.values())
    fp = sum(d["fp"] for d in per_cat.values())
    fn = sum(d["fn"] for d in per_cat.values())
    mp, mr, mf = prf(tp, fp, fn)
    active = [r for r in rows if r["tp"] + r["fn"]]
    macro = {"precision": sum(r["precision"] for r in active) / len(active),
             "recall": sum(r["recall"] for r in active) / len(active)}
    return {"micro": {"precision": mp, "recall": mr, "f1": mf,
                      "tp": tp, "fp": fp, "fn": fn},
            "macro": macro, "per_category": rows,
            "misses": misses, "false_positives": fps}


def print_report(name: str, res: dict) -> None:
    print(f"\n=== {name} ===")
    print(f"{'category':10} {'P':>6} {'R':>6} {'F1':>6} {'tp':>4} {'fp':>4} {'fn':>4} {'exact':>6}")
    for r in res["per_category"]:
        print(f"{r['category']:10} {r['precision']:6.3f} {r['recall']:6.3f} "
              f"{r['f1']:6.3f} {r['tp']:4d} {r['fp']:4d} {r['fn']:4d} {r['exact']:6d}")
    mi, ma = res["micro"], res["macro"]
    print(f"{'MICRO':10} {mi['precision']:6.3f} {mi['recall']:6.3f} {mi['f1']:6.3f} "
          f"{mi['tp']:4d} {mi['fp']:4d} {mi['fn']:4d}")
    print(f"{'MACRO':10} {ma['precision']:6.3f} {ma['recall']:6.3f}")
    if res["misses"]:
        print("misses:")
        for rid, cat, val, text in res["misses"][:8]:
            print(f"  {rid} {cat}: {val!r} in {text[:90]!r}")
    if res["false_positives"]:
        print("false positives:")
        for rid, p, val in res["false_positives"][:8]:
            print(f"  {rid} {p['category']}: {val!r}")


# ------------------------------------------------------------ main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--predict", default="regex,rubert,hybrid",
                    help="кандидаты через запятую: regex,rubert,gliner,llm,hybrid")
    ap.add_argument("--eval-only", action="store_true")
    ap.add_argument("--save", action="store_true", help="сохранить предсказания в preds/")
    ap.add_argument("--llm-batch", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    corpus = load_corpus()
    if args.limit:
        corpus = corpus[:args.limit]
    gold = load_corpus() if not args.limit else corpus
    PRED_DIR.mkdir(parents=True, exist_ok=True)
    names = [] if args.eval_only else [n.strip() for n in args.predict.split(",") if n.strip()]
    stacks: dict[str, dict[str, list[dict]]] = {}
    meta: dict[str, dict] = {}

    for name in names:
        cache = PRED_DIR / f"{name}.jsonl"
        if name == "llm":
            preds, stats = cand_llm_all(corpus, args.llm_batch)
            meta["llm"] = stats
        elif name == "gliner":
            preds = {}
            t0 = time.monotonic()
            for r in corpus:
                preds[r["id"]] = cand_gliner(r["text"])
            meta["gliner"] = {"seconds_total": time.monotonic() - t0,
                              "per_context_seconds": (time.monotonic() - t0) / len(corpus)}
        else:
            fn = {"regex": cand_regex, "rubert": cand_rubert,
                  "hybrid": cand_hybrid}[name]
            preds = {}
            t0 = time.monotonic()
            for r in corpus:
                preds[r["id"]] = fn(r["text"])
            meta[name] = {"seconds_total": time.monotonic() - t0}
        stacks[name] = preds
        if args.save or name in ("llm", "gliner"):
            with open(cache, "w", encoding="utf-8") as f:
                for rid, spans in preds.items():
                    f.write(json.dumps({"id": rid, "spans": spans},
                                       ensure_ascii=False) + "\n")
            print(f"predictions -> {cache}")

    if args.eval_only:
        for path in sorted(PRED_DIR.glob("*.jsonl")):
            preds = {r["id"]: r["spans"]
                     for r in (json.loads(x) for x in path.read_text(encoding="utf-8").splitlines())}
            stacks[path.stem] = preds

    for name, preds in stacks.items():
        res = evaluate(gold, preds)
        print_report(name, res)
        if name in meta:
            print("meta:", json.dumps(meta[name], ensure_ascii=False))
        if args.save:
            out = PRED_DIR / f"{name}.report.json"
            out.write_text(json.dumps({**res, "meta": meta.get(name, {})},
                                      ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"report -> {out}")


if __name__ == "__main__":
    main()
