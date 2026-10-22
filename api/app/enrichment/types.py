"""Типы обогащения: документ источника, вердикты критериев, доказательства.

Все dataclass'ы сериализуются в JSONB как есть (asdict) — исключение только
Evidence: цитата обязана быть точной подстрокой сохранённого текста
(проверяется в appraisal/criteria, не «по смыслу»).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Verdict = Literal["met", "not_met", "insufficient_info", "not_checked"]

VERDICT_MET: Verdict = "met"
VERDICT_NOT_MET: Verdict = "not_met"
VERDICT_INSUFFICIENT: Verdict = "insufficient_info"
VERDICT_NOT_CHECKED: Verdict = "not_checked"

Confidence = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class Evidence:
    """Подтверждающий фрагмент: точная подстрока текста источника.

    locator — доступный указатель (раздел абстракта, диапазон символов полного
    текста). page недоступен: WHO IRIS отдаёт текстовый bitstream без разметки
    страниц, поэтому честно указываем offset, а не выдумываем номер страницы.
    """

    quote: str
    locator: str
    source_url: str = ""


@dataclass(frozen=True)
class CriterionVerdict:
    key: str
    label: str
    status: Verdict
    explanation: str
    evidence: Evidence | None = None
    confidence: Confidence = "high"
    manual_review: bool = False
    manual_review_reason: str = ""


@dataclass(frozen=True)
class DesignResult:
    design: str
    confidence: Confidence
    rationale: str
    matched_rules: tuple[str, ...] = ()


@dataclass(frozen=True)
class AppraisalItem:
    key: str
    label: str
    status: Verdict
    explanation: str
    evidence: Evidence | None = None


@dataclass(frozen=True)
class AppraisalResult:
    tool: str  # amstar2 | rob2 | robins_i | agree2 | none
    tool_label: str
    applicable: bool
    reason: str
    items: tuple[AppraisalItem, ...] = ()
    overall: str = "not_assessed"  # high|moderate|low|critically_low|not_assessed
    experimental: bool = True
    summary: str = ""


@dataclass(frozen=True)
class SourceDocument:
    """Нормализованный документ из внешней базы (до сохранения в реестр)."""

    source: str  # pubmed | who
    kind: str  # article | guideline
    external_id: str  # PMID | IRIS handle
    guideline_id: str  # идентификатор чанков: pmid-123 / who-10665-374250
    title: str
    revision: str
    text: str  # статья: abstract + results; гайдлайн: полный текст
    abstract: str = ""
    results: str = ""
    journal: str = ""
    year: int | None = None
    authors: tuple[str, ...] = ()
    doi: str = ""
    pmid: str = ""
    pmcid: str = ""
    url: str = ""
    fulltext_url: str = ""
    license: str = ""
    language: str = ""
    publication_types: tuple[str, ...] = ()
    retraction: str = "unknown"  # none|retracted|correction|unknown
    retraction_note: str = ""
    fetched_at: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def content_hash_basis(self) -> str:
        """Что считаем содержимым версии (не метаданные доступа)."""
        return "\n".join(
            [
                self.title.strip(),
                self.abstract.strip(),
                self.results.strip(),
                self.text.strip() if self.kind == "guideline" else "",
            ]
        )
