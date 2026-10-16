"""ИИ-агент врача: гибридный поиск по базе знаний + ответ с цитатами.

Чистый модуль без I/O (docs/08). LLM/реранкер — за SPI (docs/03 §1.3):
по умолчанию детерминированная сборка ответа из найденных фрагментов.
Красные линии (docs/01): агент НЕ ставит диагноз и НЕ назначает/корректирует
терапию — при таком запросе отвечает отказом с кодом red line.

Контракт: docs/05-SDD.md; тесты: docs/06-TDD.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping, Sequence

# Красные линии: агент только извлекает справочную информацию из корпуса,
# но не формирует самостоятельный клинический вывод (диагноз/терапия — врач).
REDLINE_DIAGNOSIS = "DIAGNOSIS_REDLINE"
REDLINE_TREATMENT = "TREATMENT_REDLINE"
REDLINE_EMPTY = "NO_KB_DATA"

# Эвристики запроса на диагноз/терапию. [ДОПУЩЕНИЕ — заменяемо при подключении LLM.]
_DIAGNOSIS_PATTERNS = (
    r"поставь диагноз",
    r"какой диагноз\b",
    r"диагностируй",
    r"подтверди диагноз",
    r"это депрессия",
    r"это тревожное расстройство",
)
_TREATMENT_PATTERNS = (
    r"назначь",
    r"какой препарат назначить",
    r"порекомендуй лечение",
    r"схему терапии",
    r"дозировку",
    r"каким лекарством лечить",
)


@dataclass(frozen=True)
class RAGChunk:
    """Фрагмент корпуса, найденный поиском. Метаданные — payload Qdrant (docs/08 §3)."""
    text: str
    source_title: str
    guideline_id: str
    topic: str = ""
    score: float = 0.0


@dataclass(frozen=True)
class Citation:
    source_title: str
    guideline_id: str
    topic: str
    score: float


@dataclass(frozen=True)
class RAGAnswer:
    answer: str
    citations: tuple[Citation, ...] = ()
    grounded: bool = True          # True — есть релевантные фрагменты
    refused: bool = False          # True — сработала красная линия
    refusal_code: str | None = None


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def detect_redline(query: str) -> str | None:
    """Возвращает код красной линии, если запрос требует диагноз/терапию."""
    q = _normalize(query)
    for pattern in _DIAGNOSIS_PATTERNS:
        if re.search(pattern, q):
            return REDLINE_DIAGNOSIS
    for pattern in _TREATMENT_PATTERNS:
        if re.search(pattern, q):
            return REDLINE_TREATMENT
    return None


def _refusal_answer(code: str) -> RAGAnswer:
    if code == REDLINE_DIAGNOSIS:
        return RAGAnswer(
            answer=(
                "Я не могу поставить диагноз — это функция врача (323-ФЗ, доктрина "
                "«врач решает»). Я работаю как справочный ассистент: отвечаю по базе "
                "клинических рекомендаций и шкал. Для постановки диагноза обратитесь "
                "к врачу или проведите скрининг по шкале (PHQ-9/GAD-7/HADS)."
            ),
            grounded=False,
            refused=True,
            refusal_code=code,
        )
    if code == REDLINE_TREATMENT:
        return RAGAnswer(
            answer=(
                "Я не назначаю и не корректирую терапию — это прерогатива врача "
                "(242-ФЗ: врач-пациент после очного приёма). Я могу процитировать "
                "инструкцию/рекомендацию по лекарству из базы, но подбор схемы — "
                "за врачом. Сформулируйте вопрос как справочный: «что указано в "
                "инструкции препарата X?»"
            ),
            grounded=False,
            refused=True,
            refusal_code=code,
        )
    return RAGAnswer(
        answer="В базе знаний нет данных по этому запросу.",
        grounded=False,
        refused=False,
        refusal_code=code,
    )


def build_rag_answer(
    query: str,
    chunks: Sequence[RAGChunk],
    *,
    max_citations: int = 3,
) -> RAGAnswer:
    """Собирает ответ из найденных фрагментов (гибридный поиск уже отработал).

    - нет релевантных фрагментов → пустой ответ (NO_KB_DATA);
    - запрос требует диагноз/терапию → отказ с красной линией;
    - иначе — связный ответ с обязательной цитатой источника (level—title).
    """
    redline = detect_redline(query)
    if redline is not None:
        return _refusal_answer(redline)

    if not chunks:
        return _refusal_answer(REDLINE_EMPTY)

    top = list(chunks)[:max_citations]
    seen: set[str] = set()
    points: list[str] = []
    citations: list[Citation] = []

    for chunk in top:
        if chunk.guideline_id in seen:
            continue
        seen.add(chunk.guideline_id)
        snippet = " ".join(chunk.text.split())[:180]
        citations.append(
            Citation(
                source_title=chunk.source_title,
                guideline_id=chunk.guideline_id,
                topic=chunk.topic,
                score=round(chunk.score, 4),
            )
        )
        points.append(f"— {snippet}")

    body = "\n".join(f"{i + 1}. {p}" for i, p in enumerate(points))
    answer = (
        "По базе знаний найдено следующее:\n" + body + "\n\n"
        "Источники: " + ", ".join(f"«{c.source_title}» ({c.guideline_id})" for c in citations) + "."
    )
    return RAGAnswer(answer=answer, citations=tuple(citations), grounded=True)


__all__ = [
    "RAGChunk",
    "Citation",
    "RAGAnswer",
    "build_rag_answer",
    "detect_redline",
    "REDLINE_DIAGNOSIS",
    "REDLINE_TREATMENT",
    "REDLINE_EMPTY",
]
