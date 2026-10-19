"""ИИ-агент врача: гибридный поиск по базе знаний + ответ с цитатами.

Чистый модуль без I/O (docs/08). LLM/реранкер — за SPI (docs/03 §1.3):
по умолчанию детерминированная сборка ответа из найденных фрагментов.
Красные линии (docs/01): агент НЕ ставит диагноз и НЕ назначает/корректирует
терапию — при таком запросе отвечает отказом с кодом red line.

Контракт: docs/05-SDD.md; тесты: docs/06-TDD.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

# Красные линии: агент только извлекает справочную информацию из корпуса,
# но не формирует самостоятельный клинический вывод (диагноз/терапия — врач).
REDLINE_DIAGNOSIS = "DIAGNOSIS_REDLINE"
REDLINE_TREATMENT = "TREATMENT_REDLINE"
REDLINE_EMPTY = "NO_KB_DATA"

# Эвристики запроса на диагноз/терапию. [ДОПУЩЕНИЕ — заменяемо при подключении LLM.]
# Назначение/коррекция — красная линия (запрет). Справка по инструкции/показаниям/
# дозировке/противопоказаниям/НЯ препарата — РАЗРЕШЕНА (задача кейса 2: «рекомендации/
# справочная информация по вариантам терапии»), идёт в обычный retrieval по KB.
_DIAGNOSIS_PATTERNS = (
    r"поставь диагноз",
    r"какой диагноз\b",
    r"диагностируй",
    r"подтверди диагноз",
    r"это депрессия",
    r"это тревожное расстройство",
)
# Только интент назначения/изменения/отмены терапии — не справка.
_TREATMENT_PATTERNS = (
    r"назначь",
    r"какой препарат назначить",
    r"порекомендуй лечение",
    r"подбери.*схем",
    r"схем[ауы]? терап",
    r"отмени\s+\S",            # «отмени препарат/терапию»
    r"скорректируй\s+\S",      # «скорректируй терапию/дозу»
    r"увеличь\s+доз",
    r"уменьши\s+доз",
    r"поменяй\s+(препарат|терап)",
)


@dataclass(frozen=True)
class RAGChunk:
    """Фрагмент корпуса, найденный поиском. Метаданные — payload Qdrant (docs/08 §3).

    year/design/source_url — только для НАУЧНЫХ-СТАТЕЙ (docs/13); у остальных пусто.
    """
    text: str
    source_title: str
    guideline_id: str
    topic: str = ""
    score: float = 0.0
    year: int | None = None
    design: str = ""
    source_url: str = ""


@dataclass(frozen=True)
class Citation:
    source_title: str
    guideline_id: str
    topic: str
    score: float
    source_url: str = ""
    design: str = ""


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
                "Я не назначаю, не отменяю и не корректирую терапию — это "
                "прерогатива врача (323-ФЗ ст.36.2, приказ Минздрава №965н; "
                "врач-врач телемедицинские консультации и консилиумы допустимы "
                "без очного приёма). Я могу дать справку по инструкции/показаниям/"
                "дозировке/противопоказаниям препарата из базы (например, "
                "«что указано в инструкции Авиандра?»), но подбор схемы — за врачом."
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


def _patient_card_text(ctx: dict) -> str:
    """Компактная текстовая сводка карты пациента для grounding ответа (чисто, без I/O).

    Формат контракта dict — services.clinician.patient_context (API-слой).
    Используется только как фактическая опора ответа; диагноз/терапию не выводит.
    """
    fio = ctx.get("fio") or "без ФИО"
    scale = ctx.get("prescribed_scale") or "—"
    last = ctx.get("last_assessment")
    trend = ctx.get("trend") or {}
    esc = ctx.get("open_escalations") or []
    diary = ctx.get("recent_diary") or []
    parts = [f"Пациент: {fio}. Назначенная шкала: {scale}."]
    if last:
        parts.append(
            f"Последний скрининг: балл {last.get('total_score')}, "
            f"тяжесть {last.get('severity')}, триаж {last.get('triage')} "
            f"({last.get('computed_at')})."
        )
    avg = trend.get("moving_avg") or []
    if avg:
        bits = [f"последнее среднее {round(avg[-1], 1)}"]
        if trend.get("delta_7") is not None:
            bits.append(f"дельта 7д {trend['delta_7']}")
        if trend.get("delta_30") is not None:
            bits.append(f"дельта 30д {trend['delta_30']}")
        if trend.get("slope") is not None:
            bits.append(f"наклон {round(trend['slope'], 2)}")
        parts.append("Тренд: " + ", ".join(bits) + ".")
    if esc:
        kinds = ", ".join(e.get("kind", "?") for e in esc)
        parts.append(f"Открытые эскалации: {len(esc)} ({kinds}).")
    if diary:
        parts.append(
            "Последние записи дневника: "
            + " | ".join((d.get("content") or "")[:80] for d in diary)
        )
    return " ".join(parts)


def _cit_repr(c: Citation) -> str:
    """Строка цитаты: у научных статей — с URL и дизайном (docs/13)."""
    extra = ""
    if c.source_url:
        extra = f", {c.source_url}"
    elif c.design:
        extra = f", {c.design}"
    return f"«{c.source_title}» ({c.guideline_id}{extra})"


def build_rag_answer(
    query: str,
    chunks: Sequence[RAGChunk],
    *,
    max_citations: int = 3,
    patient_context: dict | None = None,
) -> RAGAnswer:
    """Собирает ответ из найденных фрагментов (гибридный поиск уже отработал).

    - запрос требует диагноз/терапию → отказ с красной линией (даже при наличии
      карты пациента — агент не ставит диагноз и не назначает терапию);
    - есть карта пациента → ответ опирается на неё + найденные фрагменты корпуса
      (карта цитируется особым источником topic=ПАЦИЕНТ, в Qdrant не попадает);
    - нет ни фрагментов, ни карты → пустой ответ (NO_KB_DATA);
    - иначе — связный ответ с обязательной цитатой источника.
    """
    redline = detect_redline(query)
    if redline is not None:
        return _refusal_answer(redline)

    has_patient = patient_context is not None
    if not chunks and not has_patient:
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
                source_url=chunk.source_url,
                design=chunk.design,
            )
        )
        points.append(f"— {snippet}")

    if has_patient:
        ctx = patient_context or {}
        patient_gid = f"patient:{ctx.get('patient_id')}"
        if patient_gid not in seen:
            seen.add(patient_gid)
            citations.append(
                Citation(
                    source_title=f"Карта пациента: {ctx.get('fio') or 'без ФИО'}",
                    guideline_id=patient_gid,
                    topic="ПАЦИЕНТ",
                    score=1.0,
                )
            )
            points.append(f"— {_patient_card_text(ctx)}")

    body = "\n".join(f"{i + 1}. {p}" for i, p in enumerate(points))
    if has_patient and chunks:
        lead = "По базе знаний и карте пациента найдено следующее:"
    elif has_patient:
        lead = "По карте пациента:"
    else:
        lead = "По базе знаний найдено следующее:"
    answer = (
        lead + "\n" + body + "\n\n"
        "Источники: " + ", ".join(_cit_repr(c) for c in citations) + "."
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
