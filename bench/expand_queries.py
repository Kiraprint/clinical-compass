"""Расширение золотого набора запросов до ~50 (RU+EN) для бенчмарка эмбеддера.

Идемпотентно: добавляет только отсутствующие id. qrels — chunk-id из corpus.jsonl,
проставлены вручную по тексту чанков (см. /tmp/corpus_full.txt при сборке).
"""
import json
import os

DATA = os.path.join(os.path.dirname(__file__), "data")

NEW = [
    # --- MCT / GAD (pmc-10277900) ---
    {"id": "q-mct-pswq-bai", "q": "Which outcome measures (PSWQ, BAI) were assessed in the metacognitive therapy for GAD meta-analysis?",
     "qrels": ["pmc-10277900-2", "pmc-10277900-3"]},
    {"id": "q-mct-recovery", "q": "Does metacognitive therapy produce higher recovery rates than CBT for generalized anxiety disorder?",
     "qrels": ["pmc-10277900-3", "pmc-10277900-4"]},
    # --- remote CBT / GAD (pmc-9790363) ---
    {"id": "q-remote-cbt-within", "q": "What is the within-group effect size of remotely delivered CBT for generalized anxiety disorder?",
     "qrels": ["pmc-9790363-2"]},
    {"id": "q-remote-cbt-between", "q": "What is the between-group effect size of remote CBT for GAD at posttreatment?",
     "qrels": ["pmc-9790363-3"]},
    # --- dCBT chronic pain (pmc-13571187) ---
    {"id": "q-dcbt-pain-intensity", "q": "Does digital cognitive behavioural therapy reduce pain intensity and pain interference in chronic pain?",
     "qrels": ["pmc-13571187-0", "pmc-13571187-2"]},
    {"id": "q-dcbt-anxiety-effect", "q": "What effect does dCBT have on anxiety symptoms in adults with chronic pain?",
     "qrels": ["pmc-13571187-3"]},
    # --- Australian DMHS (pmc-13641322) ---
    {"id": "q-dmhs-reach", "q": "How many consumers did the three Australian digital mental health services reach?",
     "qrels": ["pmc-13641322-3"]},
    {"id": "q-dmhs-cohen-d", "q": "What were the clinician-supported Cohen d effect sizes of MindSpot and THIS WAY UP?",
     "qrels": ["pmc-13641322-4"]},
    # --- curcumin (pmc-13653422) ---
    {"id": "q-curcumin-rcts", "q": "How many RCTs and participants were included in the curcumin depression meta-analysis?",
     "qrels": ["pmc-13653422-3"]},
    {"id": "q-curcumin-dose-response", "q": "Does a higher curcumin dose produce greater reductions in depressive symptoms?",
     "qrels": ["pmc-13653422-3", "pmc-13653422-4"]},
    # --- DHI / coronary heart disease (pmc-13655782) ---
    {"id": "q-dhi-sucra-qol", "q": "Which digital health intervention modality had the highest SUCRA for quality of life in coronary heart disease?",
     "qrels": ["pmc-13655782-4", "pmc-13655782-5"]},
    {"id": "q-dhi-web-anxiety", "q": "Which digital intervention platform was best for long-term anxiety and depression?",
     "qrels": ["pmc-13655782-5", "pmc-13655782-6"]},
    {"id": "q-dhi-sms-safety", "q": "Are passive SMS interventions associated with safety concerns?",
     "qrels": ["pmc-13655782-9"]},
    # --- Tai Chi (pmc-13660419) ---
    {"id": "q-taichi-overall-effect", "q": "What is the overall effect of Tai Chi on depression and anxiety symptoms?",
     "qrels": ["pmc-13660419-2", "pmc-13660419-6"]},
    {"id": "q-taichi-environment", "q": "Does the physical exercise environment moderate the effect of Tai Chi on depression and anxiety?",
     "qrels": ["pmc-13660419-1", "pmc-13660419-2"]},
    # --- aerobic exercise MDD (pmc-13264932) ---
    {"id": "q-aerobic-picos", "q": "What PICOS criteria define the aerobic exercise for major depressive disorder systematic review?",
     "qrels": ["pmc-13264932-3", "pmc-13264932-4"]},
    {"id": "q-aerobic-outcomes", "q": "What are the primary and secondary outcomes of the aerobic exercise for MDD review?",
     "qrels": ["pmc-13264932-5"]},
    {"id": "q-aerobic-stage", "q": "At what stage is the aerobic exercise for MDD systematic review and when is data extraction expected?",
     "qrels": ["pmc-13264932-7", "pmc-13264932-8"]},
    # --- WHO depression (EN) ---
    {"id": "q-who-dep-prevalence", "q": "What percentage of adults globally suffer from depression?",
     "qrels": ["who-depression-en-0", "who-depression-en-2"]},
    {"id": "q-who-dep-women", "q": "How much more common is depression among women than among men?",
     "qrels": ["who-depression-en-2"]},
    {"id": "q-who-dep-symptoms", "q": "What are the symptoms and patterns of a depressive episode?",
     "qrels": ["who-depression-en-3", "who-depression-en-4"]},
    {"id": "q-who-dep-episode-types", "q": "What patterns of depressive episodes exist (single, recurrent, bipolar)?",
     "qrels": ["who-depression-en-5"]},
    {"id": "q-who-dep-risk-factors", "q": "What social and psychological factors contribute to depression?",
     "qrels": ["who-depression-en-6"]},
    {"id": "q-who-dep-ssri", "q": "Which antidepressants are used for depression and what are SSRI examples?",
     "qrels": ["who-depression-en-9", "who-depression-en-10"]},
    {"id": "q-who-dep-children", "q": "Are antidepressant medicines recommended for children and adolescents?",
     "qrels": ["who-depression-en-10"]},
    {"id": "q-who-dep-selfcare", "q": "What self-care steps help manage symptoms of depression?",
     "qrels": ["who-depression-en-11"]},
    {"id": "q-who-dep-suicide", "q": "What should a person do if they have thoughts of suicide?",
     "qrels": ["who-depression-en-12"]},
    {"id": "q-who-dep-pmplus", "q": "What is Problem Management Plus (PM+) and who can deliver it?",
     "qrels": ["who-depression-en-14"]},
    # --- WHO anxiety (EN) ---
    {"id": "q-who-anx-prevalence-en", "q": "What is the global prevalence of anxiety disorders?",
     "qrels": ["who-anxiety-en-0", "who-anxiety-en-1"]},
    {"id": "q-who-anx-treatment-gap", "q": "What proportion of people with anxiety disorders receive any treatment?",
     "qrels": ["who-anxiety-en-1", "who-anxiety-en-2"]},
    {"id": "q-who-anx-symptoms-en", "q": "What physical and cognitive symptoms accompany anxiety disorders?",
     "qrels": ["who-anxiety-en-1", "who-anxiety-en-3"]},
    {"id": "q-who-anx-types", "q": "What types of anxiety disorders are listed (GAD, panic, social anxiety, specific phobias)?",
     "qrels": ["who-anxiety-en-4", "who-anxiety-en-5"]},
    {"id": "q-who-anx-prevention", "q": "What community-based approaches help prevent anxiety disorders?",
     "qrels": ["who-anxiety-en-7"]},
    # --- WHO anxiety (RU) ---
    {"id": "q-ru-anx-symptoms-physical", "q": "Какие физические симптомы характерны для тревожных расстройств?",
     "qrels": ["who-anxiety-ru-3", "who-anxiety-ru-4"]},
    {"id": "q-ru-anx-types", "q": "Какие разновидности тревожных расстройств выделяют?",
     "qrels": ["who-anxiety-ru-5", "who-anxiety-ru-6"]},
    {"id": "q-ru-anx-prevention", "q": "Какие методы профилактики тревожных состояний существуют?",
     "qrels": ["who-anxiety-ru-9"]},
    # --- cross-lingual RU -> EN ---
    {"id": "q-ru-dmhs", "q": "Насколько эффективны австралийские государственные цифровые службы психического здоровья?",
     "qrels": ["pmc-13641322-3", "pmc-13641322-4"]},
    {"id": "q-ru-curcumin", "q": "Как влияет прием куркумина на симптомы депрессии и тревоги?",
     "qrels": ["pmc-13653422-3", "pmc-13653422-4"]},
    {"id": "q-ru-mct", "q": "Сравнение мета-когнитивной терапии и когнитивно-поведенческой терапии при генерализованном тревожном расстройстве",
     "qrels": ["pmc-10277900-0", "pmc-10277900-3", "pmc-10277900-4"]},
    {"id": "q-ru-who-dep", "q": "Какая доля взрослых в мире страдает депрессией?",
     "qrels": ["who-depression-en-0", "who-depression-en-2"]},
    {"id": "q-ru-dhi-anxiety", "q": "Какие цифровые вмешательства лучше всего снижают тревогу при болезнях сердца?",
     "qrels": ["pmc-13655782-5", "pmc-13655782-6"]},
]


def main():
    path = os.path.join(DATA, "queries.jsonl")
    rows = [json.loads(l) for l in open(path, encoding="utf-8")]
    have = {r["id"] for r in rows}
    corpus = {json.loads(l)["id"] for l in open(os.path.join(DATA, "corpus.jsonl"), encoding="utf-8")}
    added = 0
    for q in NEW:
        if q["id"] in have:
            continue
        bad = [c for c in q["qrels"] if c not in corpus]
        assert not bad, f"{q['id']}: неизвестные qrels {bad}"
        assert q["qrels"], f"{q['id']}: пустые qrels"
        rows.append(q)
        have.add(q["id"])
        added += 1
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"добавлено {added}, всего запросов: {len(rows)}")


if __name__ == "__main__":
    main()
