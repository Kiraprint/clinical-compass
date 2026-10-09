"""Скоринг и риск-флаги. Чистые функции; ошибки — подклассы ValueError (API → 422)."""

from __future__ import annotations

from typing import Mapping

from compass.models import RiskFlag, Scale, ScaleResult


class UnknownItemError(ValueError):
    """Ответ содержит пункт, отсутствующий в шкале."""


class IncompleteAnswersError(ValueError):
    """Не все пункты шкалы заполнены."""


def score(scale: Scale, answers: Mapping[str, int]) -> ScaleResult:
    """Считает балл шкалы. Детерминированно; error-first валидация.

    Инварианты: total_score == sum по items; обратные пункты (HADS) переворачиваются.
    """
    keys = scale.item_keys()
    for key in answers:
        if key not in keys:
            raise UnknownItemError(f"unknown item {key!r} for scale {scale.code}")
    if len(answers) != len(scale.items):
        raise IncompleteAnswersError(f"expected {len(scale.items)} answers, got {len(answers)}")

    per_item: dict[str, int] = {}
    total = 0
    for item in scale.items:
        raw = answers[item.key]
        if raw < 0 or raw > item.max_value:
            raise ValueError(f"value {raw} out of range 0..{item.max_value} for item {item.key}")
        value = item.max_value - raw if item.reverse else raw
        per_item[item.key] = value
        total += value
    return ScaleResult(code=scale.code, total_score=total, severity=scale.severity_for(total), per_item=per_item)


def risk_flags(scale: Scale, answers: Mapping[str, int]) -> list[RiskFlag]:
    """Клинические риск-флаги по ответам. Сейчас: суицидальные мысли (PHQ-9, п.9)."""
    if scale.suicidal_item and answers.get(scale.suicidal_item, 0) >= 1:
        return [RiskFlag(kind="SUICIDAL_IDEATION", item_key=scale.suicidal_item)]
    return []


__all__ = ["score", "risk_flags", "UnknownItemError", "IncompleteAnswersError"]