"""Оценка методологического качества по дизайну (ТЗ п. 4–5).

Инструмент выбирается дизайном, а не «универсальным баллом»:
SR/MA → AMSTAR 2; РКИ → RoB 2; нерандомизированные → ROBINS-I;
гайдлайн → AGREE II. На абстракте возможна только частичная оценка AMSTAR 2:
пункты, требующие полного текста, помечаются `insufficient_info` (не «не
выполнено»). Итоговая уверенность помечается экспериментальной: она не
валидирована и не заменяет ревью врача.

Каждый подтверждённый пункт несёт точную подстроку абстракта (Evidence) —
цитаты не генерируются моделью.
"""

from __future__ import annotations

import re

from app.enrichment.criteria import _sentence
from app.enrichment.design import SR_FAMILY
from app.enrichment.types import (
    VERDICT_INSUFFICIENT,
    VERDICT_MET,
    AppraisalItem,
    AppraisalResult,
    DesignResult,
    Evidence,
    SourceDocument,
)

AMSTAR2_LABELS: dict[str, str] = {
    "pico": "PICO-компоненты в вопросе обзора",
    "protocol": "Протокол зарегистрирован до начала обзора",
    "design_rationale": "Обоснование выбора дизайнов включённых исследований",
    "search": "Полнота стратегии поиска (базы данных)",
    "selection_duplicate": "Отбор исследований двумя рецензентами",
    "extraction_duplicate": "Извлечение данных двумя рецензентами",
    "excluded_list": "Список исключённых исследований и причины",
    "describe_included": "Описание характеристик включённых исследований",
    "rob": "Оценка риска смещения включённых исследований",
    "funding_included": "Источники финансирования включённых исследований",
    "meta_methods": "Корректность методов метаанализа",
    "rob_impact": "Влияние риска смещения на результаты",
    "rob_interpretation": "Учёт риска смещения в интерпретации",
    "heterogeneity": "Объяснение гетерогенности",
    "publication_bias": "Оценка публикационного смещения",
    "coi": "Конфликт интересов и финансирование обзора",
}

# Порядок — канонический AMSTAR 2 (1–16).
AMSTAR2_ORDER = tuple(AMSTAR2_LABELS)

CRITICAL_ITEMS = frozenset(
    {"protocol", "search", "excluded_list", "rob", "meta_methods", "rob_interpretation", "publication_bias"}
)

_PATTERNS: dict[str, re.Pattern[str]] = {
    "protocol": re.compile(r"PROSPERO|CRD\s?\d{4,}|registered (systematic review )?protocol|OSF preregistr", re.I),
    "search": re.compile(r"PubMed|MEDLINE|Embase|Cochrane( Library)?|Web of Science|Scopus|PsycINFO|CINAHL", re.I),
    "describe_included": re.compile(r"\b\d{1,4}\s+(studies|trials|RCTs|articles|cohorts|participants from)\b", re.I),
    "rob": re.compile(r"risk of bias|\bRoB\b|Newcastle[–-]Ottawa|Cochrane (risk|RoB)|GRADE", re.I),
    "meta_methods": re.compile(r"random[- ]effects|fixed[- ]effects|\bpooled\b|DerSimonian|\bI²\b|\bI2\b", re.I),
    "heterogeneity": re.compile(r"heterogeneity|heterogeneous|\bI²\b|\bI2\b|subgroup|meta[- ]regression|sensitivity analys", re.I),
    "publication_bias": re.compile(r"publication bias|funnel plot|Egger|trim[- ]and[- ]fill", re.I),
    "coi": re.compile(r"conflict of interest|competing interest|declaration of interest|funding", re.I),
}

_POPULATION = re.compile(
    r"\b(patients|adults|children|adolescents|participants|people|women|men|older adults)\b", re.I
)
_INTERVENTION = re.compile(
    r"\b(intervention|treatment|therapy|psychotherapy|CBT|cognitive[- ]behaviou?r|antidepressant|"
    r"drug|medication|exercise|mindfulness|training|programme|program)\w*\b",
    re.I,
)
_OUTCOME = re.compile(
    r"\b(outcome|efficacy|effectiveness|remission|response rate|symptom|severity|score|quality of life|"
    r"suicidal|relapse)\w*\b",
    re.I,
)

_FULLTEXT_ONLY = {
    "design_rationale",
    "selection_duplicate",
    "extraction_duplicate",
    "excluded_list",
    "funding_included",
    "rob_impact",
    "rob_interpretation",
}


def _evidence(text: str, pattern: re.Pattern[str], url: str) -> Evidence | None:
    match = pattern.search(text or "")
    if not match:
        return None
    return Evidence(quote=_sentence(text, match.start(), match.end()), locator="abstract", source_url=url)


def _item(key: str, status: str, explanation: str, evidence: Evidence | None = None) -> AppraisalItem:
    return AppraisalItem(
        key=key,
        label=AMSTAR2_LABELS[key],
        status=status,  # type: ignore[arg-type]
        explanation=explanation,
        evidence=evidence,
    )


def _abstract_item(key: str, abstract: str, url: str, found: bool, explanation_met: str) -> AppraisalItem:
    if found:
        evidence = _evidence(abstract, _PATTERNS[key], url)
        return _item(key, VERDICT_MET, explanation_met, evidence)
    return _item(
        key,
        VERDICT_INSUFFICIENT,
        "В абстракте не сообщается; отсутствие в абстракте не означает отсутствие "
        "в полном тексте — требуется проверка первоисточника.",
    )


def appraise(doc: SourceDocument, design: DesignResult) -> AppraisalResult:
    if design.design in SR_FAMILY:
        return _appraise_amstar2(doc)
    if design.design == "rct":
        return AppraisalResult(
            tool="rob2",
            tool_label="RoB 2",
            applicable=False,
            reason="Оценка RoB 2 требует полного текста и разметки доменов; в фиде только SR/MA.",
        )
    if design.design in {"cohort", "case_control", "cross_sectional"}:
        return AppraisalResult(
            tool="robins_i",
            tool_label="ROBINS-I",
            applicable=False,
            reason="Оценка ROBINS-I требует полного текста; автоматически не выполняется.",
        )
    if design.design == "guideline":
        return AppraisalResult(
            tool="agree2",
            tool_label="AGREE II",
            applicable=False,
            reason=(
                "Официальный гайдлайн ВОЗ: автоматическая оценка AGREE II не выполняется, "
                "документ публикуется как есть с указанием редакции и лицензии."
            ),
        )
    return AppraisalResult(
        tool="none",
        tool_label="—",
        applicable=False,
        reason=f"Для дизайна «{design.design}» валидированный инструмент не выбран.",
    )


def _appraise_amstar2(doc: SourceDocument) -> AppraisalResult:
    abstract = doc.abstract or ""
    url = doc.url
    items: list[AppraisalItem] = []

    pico_found = 0
    for pattern in (_POPULATION, _INTERVENTION, _OUTCOME):
        if pattern.search(abstract):
            pico_found += 1
    if pico_found == 3:
        evidence = _evidence(abstract, _POPULATION, url)
        items.append(_item("pico", VERDICT_MET, "В абстракте присутствуют популяция, вмешательство и исход.", evidence))
    else:
        items.append(
            _item(
                "pico",
                VERDICT_INSUFFICIENT,
                f"В абстракте найдено {pico_found}/3 компонентов PICO (популяция/вмешательство/исход).",
            )
        )

    db_hits = {m.group(0).lower() for m in _PATTERNS["search"].finditer(abstract)}
    if len(db_hits) >= 2:
        items.append(
            _item(
                "search",
                VERDICT_MET,
                f"В абстракте названы базы поиска: {', '.join(sorted(db_hits))}.",
                _evidence(abstract, _PATTERNS["search"], url),
            )
        )
    else:
        items.append(
            _item(
                "search",
                VERDICT_INSUFFICIENT,
                f"Базы поиска в абстракте: {len(db_hits)} (< 2) — полнота стратегии не подтверждена.",
            )
        )

    for key in ("protocol", "describe_included", "rob", "meta_methods", "heterogeneity", "publication_bias", "coi"):
        found = bool(_PATTERNS[key].search(abstract))
        met_text = {
            "protocol": "Регистрация протокола указана в абстракте.",
            "describe_included": "Число включённых исследований указано в абстракте.",
            "rob": "Оценка риска смещения упомянута в абстракте.",
            "meta_methods": "Метод объединения/модель указаны в абстракте.",
            "heterogeneity": "Гетерогенность/подгруппы упомянуты в абстракте.",
            "publication_bias": "Публикационное смещение оценено (упомянуто в абстракте).",
            "coi": "Конфликт интересов/финансирование указаны в абстракте.",
        }[key]
        items.append(_abstract_item(key, abstract, url, found, met_text))

    for key in sorted(_FULLTEXT_ONLY, key=AMSTAR2_ORDER.index):
        items.append(
            _item(
                key,
                VERDICT_INSUFFICIENT,
                "Пункт AMSTAR 2 проверяется только по полному тексту; в продукте хранится абстракт.",
            )
        )

    items.sort(key=lambda item: AMSTAR2_ORDER.index(item.key))
    met = sum(1 for item in items if item.status == VERDICT_MET)
    critical_met = sum(1 for item in items if item.key in CRITICAL_ITEMS and item.status == VERDICT_MET)
    # По абстракту AMSTAR 2 не даёт права на «moderate/high»: 7 критических
    # пунктов проверяются только по полному тексту. Поэтому уверенность всегда
    # critically_low, а число подтверждённых пунктов — в summary.
    overall = "critically_low"
    summary = (
        f"AMSTAR 2 по абстракту: подтверждено {met}/16 пунктов "
        f"(критических {critical_met}/{len(CRITICAL_ITEMS)}). "
        "Оценка невалидирована (без полного текста и без ревью) — использовать как чек-лист, не как балл."
    )
    return AppraisalResult(
        tool="amstar2",
        tool_label="AMSTAR 2 (абстракт)",
        applicable=True,
        reason="Систематический обзор/метаанализ: применим AMSTAR 2; полный текст не хранится.",
        items=tuple(items),
        overall=overall,
        experimental=True,
        summary=summary,
    )
