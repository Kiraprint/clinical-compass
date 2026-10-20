#!/usr/bin/env python3
"""Построение бенчмарк-датасета для оценки качества поиска по базе знаний.

Корпус — реальные открытые источники по психиатрии (тревога/депрессия):
- 8 мета-анализов/систематических обзоров из PMC (open access, EN);
- факт-листы ВОЗ "Depressive disorder" (EN) и "Anxiety disorders" (EN/RU).

Каждый источник режется на чанки ~500 знаков (абзацы вхождения). Запросы —
клинические вопросы, которые врач может задать ассистенту; qrels — чанк(и)
соответствующего источника, который должен быть найден (релевантность по
содержанию текста, а не по совпадению слов). Данные оформлены так, чтобы
бенчмарк было можно расширять (добавлять источники/запросы в JSON-файлы).

Файлы вывода (в bench/data/):
- corpus.jsonl  : {"id","text","source","lang","topic"}
- queries.jsonl : {"id","q","lang","qrels":[doc_id,...],"note"}

Запуск: python bench/build_dataset.py  (перечитывает /tmp/*.json при наличии).
"""

from __future__ import annotations

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
SRC = os.path.join(HERE, "sources")
os.makedirs(DATA, exist_ok=True)


def _chunk(text: str, size: int = 500, overlap: int = 60) -> list[str]:
    """Нарезка текста на перекрывающиеся чанки ~size знаков (по абзацам)."""
    text = re.sub(r"\s+", " ", text).strip()
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # не режем посреди предложения
            sp = text.rfind(". ", start + size // 2, end)
            if sp != -1:
                end = sp + 1
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def _load_pmc() -> list[dict]:
    path = os.path.join(SRC, "pmc_meta.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    docs = []
    for r in rows:
        blank = r.get("abstract") or ""
        if not blank:
            continue
        for i, ch in enumerate(_chunk(blank)):
            docs.append(
                {
                    "id": f"pmc-{r['pmcid']}-{i}",
                    "text": ch,
                    "source": f"PMC {r['pmcid']}: {r['title']}",
                    "lang": "en",
                    "topic": "metaanalysis",
                }
            )
    return docs


def _load_who() -> list[dict]:
    path = os.path.join(SRC, "who.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    docs = []
    # ключи: depression_en, anxiety_en, anxiety_ru
    for key, lines in data.items():
        _, lang = key.rsplit("_", 1)
        kind = "depression" if key.startswith("depression") else "anxiety"
        # собираем связный текст из непустых строк, пропуская навигационные
        text_lines = [l for l in lines if len(l) > 20 and not l.startswith("Все")]
        body = " ".join(text_lines)
        for i, ch in enumerate(_chunk(body, 700, 80)):
            docs.append(
                {
                    "id": f"who-{kind}-{lang}-{i}",
                    "text": ch,
                    "source": f"WHO fact sheet: {kind} ({lang})",
                    "lang": lang,
                    "topic": "guideline",
                }
            )
    return docs


# Золотые запросы: qrels указывают на doc_id корпуса (см. принцип построения выше).
QUERIES = [
    # --- EN: мета-анализы --------------------------------------------------
    {"id": "q-mct-vs-cbt-gad", "q": "How effective is metacognitive therapy compared with cognitive behavioral therapy for generalized anxiety disorder?",
     "lang": "en", "qrels": ["pmc-10277900-0", "pmc-10277900-1", "pmc-10277900-2"],
     "note": "MCT vs CBT meta-analysis for GAD (PMC 10277900)."},
    {"id": "q-remote-cbt-gad", "q": "What is the evidence for remotely delivered cognitive behavioral therapy in generalized anxiety disorder?",
     "lang": "en", "qrels": ["pmc-9790363-0", "pmc-9790363-1", "pmc-9790363-2"],
     "note": "Remote CBT for GAD meta-analysis (PMC 9790363)."},
    {"id": "q-dcbt-chronic-pain", "q": "Does digital cognitive behavioral therapy reduce pain, anxiety and depression in adults with chronic pain?",
     "lang": "en", "qrels": ["pmc-13571187-0", "pmc-13571187-1", "pmc-13571187-2"],
     "note": "dCBT for co-occurring pain/anxiety/depression (PMC 13571187)."},
    {"id": "q-australian-dmhs", "q": "How do Australian government-funded digital mental health services perform for depression and anxiety?",
     "lang": "en", "qrels": ["pmc-13641322-0", "pmc-13641322-1"],
     "note": "Australian DMHS meta-analysis (PMC 13641322)."},
    {"id": "q-curcumin", "q": "What is the effect of curcumin supplementation on depression, anxiety and stress symptoms?",
     "lang": "en", "qrels": ["pmc-13653422-0", "pmc-13653422-1"],
     "note": "Curcumin quelate/GRADE review (PMC 13653422)."},
    {"id": "q-digital-cancer-cm", "q": "Do digital health interventions improve quality of life and reduce anxiety and depression in cancer survivors?",
     "lang": "en", "qrels": ["pmc-13655782-0", "pmc-13655782-1"],
     "note": "Digital health delivery modalities in cancer (PMC 13655782)."},
    {"id": "q-taichi", "q": "Does the exercise environment affect how tai chi influences depression and anxiety?",
     "lang": "en", "qrels": ["pmc-13660419-0", "pmc-13660419-1"],
     "note": "Tai Chi environment moderation (PMC 13660419)."},
    {"id": "q-aerobic-exercise-mdd", "q": "How does aerobic exercise affect depressive symptoms and quality of life in major depressive disorder?",
     "lang": "en", "qrels": ["pmc-13264932-0", "pmc-13264932-1", "pmc-13264932-2"],
     "note": "Aerobic exercise for MDD (PMC 13264932)."},
    # --- WHO ----------------------------------------------------------------
    {"id": "q-who-dep-epi", "q": "What is the global prevalence and burden of depressive disorder according to the WHO?",
     "lang": "en", "qrels": ["who-depression-en-0", "who-depression-en-1"],
     "note": "WHO depression fact sheet epidemiology."},
    {"id": "q-who-anx-symptoms", "q": "What are the typical symptoms and manifestations of anxiety disorders?",
     "lang": "en", "qrels": ["who-anxiety-en-2", "who-anxiety-en-3"],
     "note": "WHO anxiety fact sheet symptoms section."},
    # --- RU: перекрёстно-языковые запросы к корпусу --------------------------
    {"id": "q-ru-anx-prevalence", "q": "Какая распространенность тревожных расстройств в мире и кто чаще страдает?",
     "lang": "ru", "qrels": ["who-anxiety-ru-0", "who-anxiety-ru-1", "who-anxiety-ru-2"],
     "note": "RU query to RU/EN anxiety factsheet (cross-lingual)."},
    {"id": "q-ru-anx-symptoms", "q": "Какие симптомы характерны для тревожных расстройств?",
     "lang": "ru", "qrels": ["who-anxiety-ru-3", "who-anxiety-ru-4"],
     "note": "RU query to WHO anxiety symptoms."},
    {"id": "q-ru-dcbt", "q": "Насколько эффективна цифровая когнитивно-поведенческая терапия при хронической боли и тревоге?",
     "lang": "ru", "qrels": ["pmc-13571187-0", "pmc-13571187-1"],
     "note": "RU query to EN dCBT meta-analysis (cross-lingual)."},
    {"id": "q-ru-taichi", "q": "Влияет ли среда физических упражнений на эффект тай-чи при тревоге и депрессии?",
     "lang": "ru", "qrels": ["pmc-13660419-0", "pmc-13660419-1"],
     "note": "RU query to EN Tai Chi meta-analysis (cross-lingual)."},
]


def main() -> None:
    docs = _load_pmc() + _load_who()
    with open(os.path.join(DATA, "corpus.jsonl"), "w", encoding="utf-8") as f:
        for d in docs:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    with open(os.path.join(DATA, "queries.jsonl"), "w", encoding="utf-8") as f:
        for q in QUERIES:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")
    # проверка целостности qrels
    ids = {d["id"] for d in docs}
    missing = [q["id"] for q in QUERIES if any(x not in ids for x in q["qrels"])]
    print(f"corpus passagen: {len(docs)}; queries: {len(QUERIES)}; broken qrels: {missing}")


if __name__ == "__main__":
    main()
