"""Классификация дизайна исследования по метаданным (детерминированно).

Основа — PublicationType PubMed плюс явные маркеры в заголовке. LLM здесь не
нужен: типы размечены NLM, ошибка классификации видна и проверяема. При
противоречии (заголовок «network meta-analysis», тип «Meta-Analysis») выбирается
более конкретный дизайн с confidence=medium.
"""

from __future__ import annotations

import re

from app.enrichment.types import DesignResult, SourceDocument

DESIGN_LABELS = {
    "network_meta_analysis": "Сетевой метаанализ",
    "umbrella_review": "Обзор обзоров (umbrella review)",
    "systematic_review_meta_analysis": "Систематический обзор с метаанализом",
    "meta_analysis": "Метаанализ",
    "systematic_review": "Систематический обзор",
    "rct": "Рандомизированное контролируемое исследование",
    "cohort": "Когортное исследование",
    "case_control": "Исследование случай–контроль",
    "cross_sectional": "Поперечное исследование",
    "guideline": "Клинический гайдлайн",
    "narrative_review": "Нарративный обзор",
    "other": "Дизайн не определён",
}

# Семейство дизайнов, для которых существует валидированный инструмент оценки
# (AMSTAR 2). Продуктовый фид — только они.
SR_FAMILY = frozenset(
    {"systematic_review", "meta_analysis", "network_meta_analysis",
     "systematic_review_meta_analysis", "umbrella_review"}
)

_TITLE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("network_meta_analysis", re.compile(r"network\s+meta-?analy", re.I)),
    ("umbrella_review", re.compile(r"umbrella\s+review|review\s+of\s+(systematic\s+)?reviews", re.I)),
    ("systematic_review_meta_analysis", re.compile(r"systematic\s+review\s+and\s+meta-?analy", re.I)),
    ("meta_analysis", re.compile(r"meta-?analy", re.I)),
    ("systematic_review", re.compile(r"systematic\s+review", re.I)),
)

_TYPE_RULES: tuple[tuple[str, frozenset[str]], ...] = (
    ("meta_analysis", frozenset({"meta-analysis", "meta analysis"})),
    ("systematic_review", frozenset({"systematic review"})),
    ("rct", frozenset({"randomized controlled trial", "randomised controlled trial", "clinical trial"})),
    ("cohort", frozenset({"observational study"})),
    ("guideline", frozenset({"guideline", "practice guideline"})),
)


def classify(doc: SourceDocument) -> DesignResult:
    if doc.kind == "guideline":
        return DesignResult(
            design="guideline",
            confidence="high",
            rationale="Документ ВОЗ из белого списка гайдлайнов (IRIS).",
            matched_rules=("kind:guideline",),
        )

    types = {t.strip().lower() for t in doc.publication_types}
    title = doc.title or ""
    matched: list[str] = []

    for design, pattern in _TITLE_RULES:
        if pattern.search(title):
            matched.append(f"title:{pattern.pattern}")
            # Заголовок уточняет тип: network/umbrella/комбинированный SR+MA.
            return DesignResult(
                design=design,
                confidence="medium" if design not in _type_implied(types, design) else "high",
                rationale=(
                    f"Заголовок содержит маркер «{pattern.pattern}»; "
                    f"PublicationType: {', '.join(sorted(types)) or 'нет'}."
                ),
                matched_rules=tuple(matched + [f"pt:{t}" for t in sorted(types)]),
            )

    for design, expected in _TYPE_RULES:
        if types & expected:
            matched.append(f"pt:{design}")
            return DesignResult(
                design=design,
                confidence="high",
                rationale=f"PublicationType PubMed: {', '.join(sorted(types & expected))}.",
                matched_rules=tuple(matched),
            )

    if "review" in types:
        return DesignResult(
            design="narrative_review",
            confidence="medium",
            rationale="PublicationType Review без маркеров систематического обзора.",
            matched_rules=("pt:review",),
        )

    return DesignResult(
        design="other",
        confidence="low",
        rationale=(
            "Ни PublicationType, ни заголовок не позволяют определить дизайн; "
            "нужна ручная проверка."
        ),
        matched_rules=(),
    )


def _type_implied(types: set[str], design: str) -> set[str]:
    for candidate, expected in _TYPE_RULES:
        if candidate == design and types & expected:
            return {design}
    if design == "systematic_review_meta_analysis" and (
        {"meta-analysis", "systematic review"} & types
    ):
        return {design}
    return set()

