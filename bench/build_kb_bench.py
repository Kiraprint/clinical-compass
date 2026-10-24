#!/usr/bin/env python3
"""Сборка набора запросов для бенчмарка БЗ (150 вопросов, RU первичный + EN).

Этапы:
1. Снимок корпуса с Qdrant (`--export`, коллекция `kb` → bench/data/kb_corpus.jsonl).
2. Ручные запросы по «хрупким» разделам: шкалы (имена/кто заполняет/подшкалы),
   препараты (дозировка/противопоказания/МАО), триаж, статьи.
3. Генерация остальных запросов LLM-шлюзом home-stable: на каждый сэмпл-чанк КР
   ВОЗ — один RU-вопрос (кросс-языковой, формулировки НЕ копируются) и один EN.
   60 сэмплов × 2 = 120 запросов.

Выход: bench/data/kb_queries.jsonl {id,q,lang,family,seed,note}.
Идемпотентно: повторный запуск с уже существующим файлом ничего не дублирует
(генерация пропускается при наличии >= MIN_GENERATED записей).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_bench as kb  # noqa: E402

N_SEEDS = 60
BATCH = 5
MIN_GENERATED = 110

# Ручные запросы: покрывают разделы, на которых лексические/плотные промахи
# видны чаще всего (имена шкал, дозирование, коморбидность, triage, check-in).
MANUAL: list[dict] = [
    # --- шкалы ---
    {"id": "m-scale-hypomania", "q": "Какой опросник используют для разового скрининга гипомании?", "lang": "ru", "family": "scales",
     "note": "HCL-32"},
    {"id": "m-scale-hypomania-en", "q": "Which self-report questionnaire screens for hypomanic symptoms?", "lang": "en", "family": "scales",
     "note": "HCL-32"},
    {"id": "m-scale-clinician-rated", "q": "Какую шкалу депрессии заполняет врач, а не пациент?", "lang": "ru", "family": "scales",
     "note": "HDRS-17"},
    {"id": "m-scale-hdrs-items", "q": "Из каких пунктов состоит шкала Гамильтона из 17 пунктов?", "lang": "ru", "family": "scales",
     "note": "HDRS-17"},
    {"id": "m-scale-hads-subscales", "q": "Какие две подшкалы входят в госпитальную шкалу тревоги и депрессии?", "lang": "ru", "family": "scales",
     "note": "HADS-A/HADS-D"},
    {"id": "m-scale-hads-anxiety-items", "q": "Какие вопросы относятся к тревожной подшкале HADS?", "lang": "ru", "family": "scales",
     "note": "HADS-A"},
    {"id": "m-scale-pss-10", "q": "Какой опросник оценивает воспринимаемый стресс за последний месяц?", "lang": "ru", "family": "scales",
     "note": "PSS-10"},
    {"id": "m-scale-phq-items", "q": "Какие симптомы депрессии оценивает опросник PHQ-9?", "lang": "ru", "family": "scales",
     "note": "PHQ-9"},
    {"id": "m-scale-gad7", "q": "Что входит в семь пунктов шкалы генерализованной тревоги?", "lang": "ru", "family": "scales",
     "note": "GAD-7"},
    {"id": "m-scale-gad7-en", "q": "What symptoms does the GAD-7 questionnaire ask about?", "lang": "en", "family": "scales",
     "note": "GAD-7"},
    # --- препараты ---
    {"id": "m-drug-sertraline-dose", "q": "Какие таблетки и в какой дозировке выпускает золофт?", "lang": "ru", "family": "drugs",
     "note": "ЗОЛОФТ 50/100 мг"},
    {"id": "m-drug-sertraline-contra", "q": "Можно ли назначать сертралин вместе с ингибиторами МАО?", "lang": "ru", "family": "drugs",
     "note": "противопоказание, 2 недели"},
    {"id": "m-drug-sertraline-ae", "q": "Какие частые нежелательные явления у сертралина?", "lang": "ru", "family": "drugs",
     "note": "тошнота, диарея, сухость во рту"},
    {"id": "m-drug-escitalopram-contra", "q": "При каких состояниях эсциталопрам противопоказан?", "lang": "ru", "family": "drugs",
     "note": "МАО, пимозид, до 18 лет"},
    {"id": "m-drug-escitalopram-mech", "q": "Как работает эсциталопрам на серотонин?", "lang": "ru", "family": "drugs",
     "note": "СИОЗС механизм"},
    {"id": "m-drug-escitalopram-dose", "q": "В каких дозировках выпускают таблетки эсциталопрама?", "lang": "ru", "family": "drugs",
     "note": "5/10/20 мг"},
    {"id": "m-drug-maritupirdine-dose", "q": "Какая лекарственная форма и дозировка у маритупирдина?", "lang": "ru", "family": "drugs",
     "note": "20 мг"},
    {"id": "m-drug-maritupirdine-contra", "q": "Можно ли маритупирдин при эпилепсии и почечной недостаточности?", "lang": "ru", "family": "drugs",
     "note": "противопоказания"},
    {"id": "m-drug-maritupirdine-mech", "q": "На какие рецепторы действует анксиолитик маритупирдин?", "lang": "ru", "family": "drugs",
     "note": "5-HT7, α2, H1"},
    {"id": "m-drug-ssri-anxiety-en", "q": "Which antidepressants are first-line for panic disorder in low-resource settings?", "lang": "en", "family": "drugs",
     "note": "mhGAP: SSRI затем TCA"},
    # --- триаж / красные линии ---
    {"id": "m-triage-red", "q": "Что означает красный триаж и что показать пациенту?", "lang": "ru", "family": "triage",
     "note": "телефоны 8-800-2000-122, 112"},
    {"id": "m-triage-suicide-en", "q": "What should be done when a patient reports suicidal thoughts?", "lang": "en", "family": "triage",
     "note": "suicide risk, WHO"},
    {"id": "m-redline-diagnosis", "q": "Может ли ассистент поставить пациенту диагноз?", "lang": "ru", "family": "triage",
     "note": "красная линия: постановка диагноза"},
    # --- статьи / коморбидность / check-in ---
    {"id": "m-art-vortioxetine", "q": "Что известно про вортиоксетин при сочетании депрессии и тревоги?", "lang": "ru", "family": "articles",
     "note": "РКИ 8 недель"},
    {"id": "m-art-trd-pseudo", "q": "Как отличить истинную резистентность от псевдорезистентной депрессии?", "lang": "ru", "family": "articles",
     "note": "клинометрический подход"},
    {"id": "m-art-trd-relapse", "q": "С чем связан повышенный риск рецидива при резистентной депрессии?", "lang": "ru", "family": "articles",
     "note": "когорта: коморбидности, ССЗ"},
    {"id": "m-art-adr", "q": "Что показывает анализ базы нежелательных реакций по антидепрессантам в РФ?", "lang": "ru", "family": "articles",
     "note": "фармаконадзор"},
    {"id": "m-checkin-followup", "q": "Как часто нужно спрашивать пациента о динамике состояния на повторных приёмах?", "lang": "ru", "family": "checkin",
     "note": "мониторинг динамики"},
    {"id": "m-checkin-improve-en", "q": "How often should treatment response be reassessed during follow-up?", "lang": "en", "family": "checkin",
     "note": "WHO follow-up"},
    {"id": "m-comorb-depression-diabetes", "q": "Как лечить депрессию у пациента с сахарным диабетом?", "lang": "ru", "family": "comorbidity",
     "note": "mhGAP physical health"},
    {"id": "m-comorb-depression-hiv", "q": "Что учитывать при депрессии у пациента с ВИЧ?", "lang": "ru", "family": "comorbidity",
     "note": "physical health conditions"},
    {"id": "m-comorb-epilepsy-depression", "q": "Можно ли антидепрессанты при эпилепсии?", "lang": "ru", "family": "comorbidity",
     "note": "epilepsy comorbidity"},
    {"id": "m-therapy-psychosis", "q": "Какие психосоциальные вмешательства рекомендуются при психозе?", "lang": "ru", "family": "guideline",
     "note": "mhGAP psychosis"},
    {"id": "m-work-burnout", "q": "Что рекомендуют работодателям для профилактики выгорания на работе?", "lang": "ru", "family": "guideline",
     "note": "WHO mental health at work"},
    {"id": "m-adolescent-selfhelp", "q": "Какие вмешательства помогают подросткам с эмоциональными проблемами?", "lang": "ru", "family": "guideline",
     "note": "helping adolescents thrive"},
    {"id": "m-dementia-caregiver", "q": "Как помочь ухаживающим за пациентом с деменцией?", "lang": "ru", "family": "guideline",
     "note": "caregiver interventions"},
    {"id": "m-alcohol-withdrawal", "q": "Что делать при синдроме отмены алкоголя?", "lang": "ru", "family": "guideline",
     "note": "mhGAP substance use"},
    {"id": "m-ptsd-first-line-en", "q": "What is the first-line psychological treatment for post-traumatic stress disorder?", "lang": "en", "family": "guideline",
     "note": "mhGAP PTSD"},
]

GEN_PROMPT = """Ниже — фрагменты клинического руководства ВОЗ по психическому здоровью (англ. язык).
Для КАЖДОГО фрагмента составь два запроса, на которые этот фрагмент отвечает:
1) "ru" — естественный вопрос русскоязычного врача ассистенту: 6–14 слов, разговорно и конкретно;
   НЕ копируй дословно редкие термины и целые формулировки фрагмента, не ссылайся на «документ/текст/фрагмент»;
2) "en" — тот же смысл естественным английским клиническим вопросом.
Верни ТОЛЬКО JSON-массив объектов:
[{"id": "<id фрагмента>", "ru": "...", "en": "..."}]
Никаких пояснений вне JSON.

Фрагменты:
"""


def _seeds(corpus: list[dict], n: int) -> list[dict]:
    """Стратифицированная выборка чанков КР: длинные, из всех источников."""
    pools: dict[str, list[dict]] = {}
    for r in corpus:
        if r.get("topic") != "РЕКОМЕНДАЦИИ":
            continue
        if len(r.get("text", "")) < 900:
            continue
        pools.setdefault((r.get("source_title") or "")[:40], []).append(r)
    rng = random.Random(20261024)
    for items in pools.values():
        rng.shuffle(items)
    keys = sorted(pools)
    out: list[dict] = []
    i = 0
    while len(out) < n and any(pools[k] for k in keys):
        k = keys[i % len(keys)]
        if pools[k]:
            out.append(pools[k].pop())
        i += 1
    return out


def _generate(corpus: list[dict], n_seeds: int) -> list[dict]:
    seeds = _seeds(corpus, n_seeds)
    batches = [seeds[i : i + BATCH] for i in range(0, len(seeds), BATCH)]
    by_id = {r["id"]: r for r in corpus}

    def run(batch: list[dict]) -> list[dict]:
        body = "\n\n".join(
            f"[{r['id']}]\n{r['text'][:1400]}" for r in batch
        )
        last: Exception | None = None
        for attempt in range(3):
            try:
                text = kb.chat_json(GEN_PROMPT + body)
                items = kb.parse_json_array(text)
                out = []
                for it in items:
                    cid = str(it.get("id", "")).strip()
                    if cid not in by_id:
                        continue
                    if it.get("ru"):
                        out.append({"q": it["ru"].strip(), "lang": "ru", "seed": cid, "family": "generated"})
                    if it.get("en"):
                        out.append({"q": it["en"].strip(), "lang": "en", "seed": cid, "family": "generated"})
                if out:
                    return out
            except Exception as exc:  # noqa: BLE001 — сеть/формат, ретраим
                last = exc
                time.sleep(2 * (attempt + 1))
        print(f"[warn] batch failed: {last}", file=sys.stderr)
        return []

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(run, batches))
    out = [q for r in results for q in r]
    # дедуп по нормализованному тексту
    seen: set[str] = set()
    uniq = []
    for q in out:
        key = q["q"].lower().strip(" ?.!")
        if key in seen:
            continue
        seen.add(key)
        uniq.append(q)
    return uniq


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", action="store_true", help="снять корпус с Qdrant в bench/data/kb_corpus.jsonl")
    ap.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    ap.add_argument("--collection", default="kb")
    ap.add_argument("--regenerate", action="store_true", help="перегенерировать LLM-часть запросов")
    args = ap.parse_args()

    if args.export or not kb.CORPUS.exists():
        n = kb.export_corpus(args.qdrant_url, args.collection, kb.CORPUS)
        print(f"корпус: {n} чанков → {kb.CORPUS}")
    corpus = kb.load_corpus()

    existing: list[dict] = []
    if kb.QUERIES.exists() and not args.regenerate:
        existing = [json.loads(x) for x in kb.QUERIES.read_text(encoding="utf-8").splitlines() if x.strip()]
    generated = [q for q in existing if q.get("family") == "generated"]
    if args.regenerate or len(generated) < MIN_GENERATED:
        generated = _generate(corpus, N_SEEDS)
        print(f"сгенерировано запросов: {len(generated)}")

    queries: list[dict] = []
    for i, q in enumerate(generated):
        queries.append({"id": f"g-{i:03d}", **q})
    queries.extend(MANUAL)
    # проверка целостности seed-ссылок
    ids = {r["id"] for r in corpus}
    missing = [q["id"] for q in queries if q.get("seed") and q["seed"] not in ids]
    if missing:
        print(f"[warn] seed-чанки не найдены: {missing[:5]}", file=sys.stderr)

    kb.QUERIES.parent.mkdir(parents=True, exist_ok=True)
    with kb.QUERIES.open("w", encoding="utf-8") as f:
        for q in queries:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")
    ru = sum(1 for q in queries if q["lang"] == "ru")
    print(f"запросов: {len(queries)} (ru={ru}, en={len(queries) - ru}) → {kb.QUERIES}")


if __name__ == "__main__":
    main()
