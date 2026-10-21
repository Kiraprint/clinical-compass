"""Критерии включения/исключения — детерминированные правила (docs/13, docs/15).

Принципы (ТЗ, п. 4–5):
- тематическая релевантность, соответствие критериям, дизайн, риск смещения,
  полнота данных и статус публикации хранятся раздельно, а не одним баллом;
- `insufficient_info` и `not_checked` НЕ равны `not_met`: неизвестное не
  считается недостатком;
- исключают только подтверждённые нарушения (not_met) жёстких критериев.

Версионируется через CRITERIA_VERSION: прогон пишет версию в kb_runs.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from app.enrichment.design import SR_FAMILY
from app.enrichment.types import (
    VERDICT_INSUFFICIENT,
    VERDICT_MET,
    VERDICT_NOT_CHECKED,
    VERDICT_NOT_MET,
    CriterionVerdict,
    DesignResult,
    Evidence,
    SourceDocument,
    Verdict,
)

FRESHNESS_YEARS = 5

# Тематическое ядро продукта: тревога/депрессия, БАР, ТРД, суицидальный риск,
# психиатрия в целом (для гайдлайнов ВОЗ широкого профиля).
_CORE_TERMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("depression", re.compile(r"\bdepress(ion|ive|ant)?\b", re.I)),
    ("anxiety", re.compile(r"\banxiet(y|ies)\b|\banxious\b", re.I)),
    ("bipolar", re.compile(r"\bbipolar\b|\bmania\b|\bmanic\b", re.I)),
    ("suicide", re.compile(r"\bsuicid\w*\b|\bself[- ]harm\w*\b", re.I)),
    ("treatment-resistant", re.compile(r"treatment[- ]resistant|refractory depression", re.I)),
    ("mental-health", re.compile(r"\bmental (health|disorder\w*|illness\w*)\b|\bpsychiatric\w*\b", re.I)),
    ("antidepressant", re.compile(r"\bantidepressant\w*\b|\bSSRIs?\b|\bSNRIs?\b", re.I)),
    ("ptsd", re.compile(r"\bpost[- ]traumatic stress\b|\bPTSD\b", re.I)),
    ("schizophrenia", re.compile(r"\bschizophreni\w*\b|\bpsychosi\w*\b", re.I)),
    ("ocd", re.compile(r"\bobsessive[- ]compulsive\b|\bOCD\b", re.I)),
    ("eating-disorder", re.compile(r"\beating disorder\w*\b|\banorexi\w*\b|\bbulimi\w*\b", re.I)),
    ("substance-use", re.compile(r"\bsubstance use\b|\balcohol use disorder\b|\bdrug use disorder\b", re.I)),
)

_LANGUAGES_OK = {"eng", "english", "ru", "rus", "russian"}
_LICENSE_OK = re.compile(r"CC BY-NC-SA 3\.0 IGO", re.I)

ARTICLE_HARD = frozenset({"psychiatry_relevance", "freshness", "design", "language", "retraction"})
GUIDELINE_HARD = frozenset({"psychiatry_relevance", "license", "text_available"})

LABELS = {
    "psychiatry_relevance": "Тематическая релевантность (психиатрия)",
    "freshness": "Свежесть публикации",
    "design": "Дизайн исследования",
    "language": "Язык публикации",
    "retraction": "Статус публикации (ретракции/исправления)",
    "reporting_completeness": "Полнота доступных данных (абстракт)",
    "license": "Лицензия допускает использование",
    "text_available": "Доступен текст для индексации",
    "recency": "Актуальность редакции (> 5 лет — предупреждение)",
    "source_authority": "Источник — официальный гайдлайн ВОЗ",
}


@dataclass(frozen=True)
class Evaluation:
    verdicts: tuple[CriterionVerdict, ...]
    decision: str  # include | exclude
    reasons: tuple[str, ...]
    relevance_score: float

    def by_key(self, key: str) -> CriterionVerdict | None:
        for verdict in self.verdicts:
            if verdict.key == key:
                return verdict
        return None


def _sentence(text: str, start: int, end: int, limit: int = 400) -> str:
    """Точная подстрока текста: предложение вокруг совпадения."""
    left = max(text.rfind(".", 0, start), text.rfind("\n", 0, start))
    right_candidates = [i for i in (text.find(".", end), text.find("\n", end)) if i != -1]
    right = min(right_candidates) + 1 if right_candidates else min(len(text), end + 120)
    left = max(0, left + 1)
    quote = text[left:right].strip()
    return quote[:limit]


def _verdict(
    key: str,
    status: Verdict,
    explanation: str,
    *,
    evidence: Evidence | None = None,
    manual_review: bool = False,
    manual_review_reason: str = "",
    confidence: str = "high",
) -> CriterionVerdict:
    return CriterionVerdict(
        key=key,
        label=LABELS[key],
        status=status,
        explanation=explanation,
        evidence=evidence,
        confidence=confidence,  # type: ignore[arg-type]
        manual_review=manual_review,
        manual_review_reason=manual_review_reason,
    )


def relevance(doc: SourceDocument) -> tuple[CriterionVerdict, float]:

    # У гайдлайна абстракт неполон; титул + первые килобайты текста дают
    # проверяемый указатель. Для статьи тело — только абстракт.
    title = doc.title or ""
    body = (doc.text or "")[:4000] if doc.kind == "guideline" else (doc.abstract or "")
    body_locator = "fulltext" if doc.kind == "guideline" else "abstract"
    found: list[str] = []
    evidence: Evidence | None = None
    for name, pattern in _CORE_TERMS:
        title_match = pattern.search(title)
        body_match = pattern.search(body)
        if not (title_match or body_match):
            continue
        found.append(name)
        if evidence is not None:
            continue
        if title_match:
            # Цитата — точная подстрока заголовка, а не склейка title+abstract.
            evidence = Evidence(
                quote=title[title_match.start() : title_match.end()],
                locator="title",
                source_url=doc.url,
            )
        elif body_match:
            evidence = Evidence(
                quote=_sentence(body, body_match.start(), body_match.end()),
                locator=body_locator,
                source_url=doc.url,
            )
    score = min(1.0, len(found) / 3)
    if found:
        status: Verdict = VERDICT_MET
        explanation = f"Найдены тематические маркеры: {', '.join(found)}."
    else:
        status = VERDICT_NOT_MET
        explanation = (
            "В заголовке, абстракте и начале текста нет маркеров психиатрической тематики "
            f"(порог: хотя бы один из {len(_CORE_TERMS)} кластеров терминов)."
        )
    return (
        _verdict("psychiatry_relevance", status, explanation, evidence=evidence),
        score,
    )


def freshness(doc: SourceDocument, today: dt.date) -> CriterionVerdict:
    if doc.year is None:
        return _verdict(
            "freshness",
            VERDICT_INSUFFICIENT,
            "Год публикации не указан в метаданных — свежесть не проверена.",
            confidence="low",
            manual_review=True,
            manual_review_reason="Неизвестный год: проверить первоисточник.",
        )
    age = today.year - doc.year
    if age <= FRESHNESS_YEARS:
        return _verdict(
            "freshness",
            VERDICT_MET,
            f"Опубликовано в {doc.year} (возраст {age} г. ≤ {FRESHNESS_YEARS}).",
        )
    return _verdict(
        "freshness",
        VERDICT_NOT_MET,
        f"Опубликовано в {doc.year}: старше {FRESHNESS_YEARS} лет (возраст {age} г.).",
    )


def design_criterion(design: DesignResult) -> CriterionVerdict:
    if design.design in SR_FAMILY:
        return _verdict(
            "design",
            VERDICT_MET,
            f"Дизайн: {design.design} ({design.rationale})",
        )
    return _verdict(
        "design",
        VERDICT_NOT_MET,
        f"Дизайн «{design.design}» вне продуктового фида (только SR/MA). {design.rationale}",
        manual_review=design.confidence == "low",
    )


def language(doc: SourceDocument) -> CriterionVerdict:
    value = (doc.language or "").strip().lower()
    if not value:
        return _verdict("language", VERDICT_INSUFFICIENT, "Язык не указан в метаданных.", confidence="low")
    if value in _LANGUAGES_OK:
        return _verdict("language", VERDICT_MET, f"Язык: {doc.language}.")
    return _verdict("language", VERDICT_NOT_MET, f"Язык {doc.language} вне корпуса (en/ru).")


def retraction(doc: SourceDocument) -> CriterionVerdict:
    if doc.retraction == "none":
        return _verdict(
            "retraction",
            VERDICT_MET,
            "По данным PubMed статья не отозвана и не имеет исправлений.",
        )
    if doc.retraction == "retracted":
        return _verdict(
            "retraction",
            VERDICT_NOT_MET,
            f"Статья отозвана: {doc.retraction_note or 'см. PubMed'}.",
            manual_review=True,
            manual_review_reason="Ретракция: проверить причину и дату.",
        )
    if doc.retraction == "correction":
        return _verdict(
            "retraction",
            VERDICT_MET,
            f"Есть исправление/уточнение: {doc.retraction_note or 'см. PubMed'}.",
            manual_review=True,
            manual_review_reason="Исправление: проверить, затрагивает ли результаты.",
        )
    return _verdict(
        "retraction",
        VERDICT_NOT_CHECKED,
        "Статус ретракции не проверен (нет данных источника).",
        confidence="low",
    )


def reporting_completeness(doc: SourceDocument) -> CriterionVerdict:
    abstract = (doc.abstract or "").strip()
    if not abstract:
        return _verdict(
            "reporting_completeness",
            VERDICT_INSUFFICIENT,
            "Абстракт отсутствует в метаданных — оценивать нечего.",
            confidence="low",
        )
    if doc.results.strip():
        return _verdict(
            "reporting_completeness",
            VERDICT_MET,
            "Структурированный абстракт содержит раздел результатов.",
        )
    return _verdict(
        "reporting_completeness",
        VERDICT_INSUFFICIENT,
        "Раздел результатов не выделен в абстракте (текст неструктурированный).",
        confidence="medium",
        manual_review=True,
        manual_review_reason="Результаты не выделены: открыть первоисточник.",
    )


def guideline_license(doc: SourceDocument) -> CriterionVerdict:
    if _LICENSE_OK.search(doc.license or ""):
        return _verdict(
            "license",
            VERDICT_MET,
            "В тексте документа указана лицензия CC BY-NC-SA 3.0 IGO "
            "(некоммерческое использование — условие продукта).",
        )
    if (doc.license or "").lower() == "proprietary":
        return _verdict(
            "license",
            VERDICT_NOT_MET,
            "В тексте указано «All rights reserved»: индексация без письменного "
            "разрешения правообладателя недопустима.",
            manual_review=True,
            manual_review_reason="Проприетарная лицензия: нужно разрешение ВОЗ.",
        )
    if doc.license:
        return _verdict(
            "license",
            VERDICT_INSUFFICIENT,
            f"Лицензия «{doc.license}» не проверена автоматически: условия "
            "индексации требуют решения человека.",
            manual_review=True,
            manual_review_reason="Нестандартная лицензия: проверить условия.",
        )
    return _verdict(
        "license",
        VERDICT_INSUFFICIENT,
        "Лицензия не найдена в тексте документа — индексация требует approve.",
        confidence="low",
        manual_review=True,
        manual_review_reason="Лицензия не подтверждена автоматически.",
    )


def text_available(doc: SourceDocument) -> CriterionVerdict:
    size = len((doc.text or "").strip())
    if size >= 5000:
        return _verdict("text_available", VERDICT_MET, f"Доступен текст {size} символов.")
    return _verdict(
        "text_available",
        VERDICT_NOT_MET,
        f"Текст короче минимума индексации ({size} < 5000 символов).",
    )


def guideline_recency(doc: SourceDocument, today: dt.date) -> CriterionVerdict:
    if doc.year is None:
        return _verdict(
            "recency",
            VERDICT_INSUFFICIENT,
            "Дата редакции неизвестна.",
            confidence="low",
            manual_review=True,
            manual_review_reason="Проверить актуальность редакции.",
        )
    age = today.year - doc.year
    if age <= FRESHNESS_YEARS:
        return _verdict("recency", VERDICT_MET, f"Редакция {doc.year} (возраст {age} г.).")
    return _verdict(
        "recency",
        VERDICT_NOT_MET,
        f"Редакция {doc.year} старше {FRESHNESS_YEARS} лет — предупреждение, не исключение.",
        manual_review=True,
        manual_review_reason="Старая редакция: проверить, не заменена ли новой.",
    )


def evaluate(doc: SourceDocument, design: DesignResult, today: dt.date | None = None) -> Evaluation:
    """Собирает вердикты и решение. Исключают только not_met жёстких критериев."""
    today = today or dt.date.today()
    relevance_verdict, relevance_score = relevance(doc)
    if doc.kind == "guideline":
        verdicts = (
            relevance_verdict,
            guideline_license(doc),
            text_available(doc),
            guideline_recency(doc, today),
            _verdict(
                "source_authority",
                VERDICT_MET,
                f"Официальный документ ВОЗ в белом списке (IRIS handle {doc.external_id}).",
            ),
        )
        hard = GUIDELINE_HARD
    else:
        verdicts = (
            relevance_verdict,
            freshness(doc, today),
            design_criterion(design),
            language(doc),
            retraction(doc),
            reporting_completeness(doc),
        )
        hard = ARTICLE_HARD

    reasons = tuple(
        f"{v.label}: {v.explanation}"
        for v in verdicts
        if v.key in hard and v.status == VERDICT_NOT_MET
    )
    decision = "exclude" if reasons else "include"
    return Evaluation(
        verdicts=verdicts,
        decision=decision,
        reasons=reasons,
        relevance_score=relevance_score,
    )
