"""Скоринг и риск-флаги. Чистые функции; ошибки — подклассы ValueError (API → 422)."""

from __future__ import annotations

from typing import Mapping

from compass.models import FlagKind, RiskFlag, Scale, ScaleResult, Severity


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
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise ValueError(f"value {raw!r} for item {item.key} is not an int (ожидается 0..{item.max_value})")  # → 422
        if raw < 0 or raw > item.max_value:
            raise ValueError(f"value {raw} out of range 0..{item.max_value} for item {item.key}")
        value = item.max_value - raw if item.reverse else raw
        per_item[item.key] = value
        total += value
    return ScaleResult(code=scale.code, total_score=total, severity=scale.severity_for(total), per_item=per_item)


def risk_flags(scale: Scale, answers: Mapping[str, int]) -> list[RiskFlag]:
    """Риск-флаги, выводимые из структуры шкалы — без привязки к расстройству:

    - SUICIDAL_IDEATION: scale.suicidal_item (пункт о суициде) >= 1
    - SEVERE_SYMPTOMS: сумма баллов достигла собственного порога SEVERE шкалы
    """
    flags: list[RiskFlag] = []
    if scale.suicidal_item and answers.get(scale.suicidal_item, 0) >= 1:
        flags.append(RiskFlag(kind=FlagKind.SUICIDAL_IDEATION, item_key=scale.suicidal_item))
    if scale.severity_for(total_for(scale, answers)) == Severity.SEVERE:
        flags.append(RiskFlag(kind=FlagKind.SEVERE_SYMPTOMS, item_key=None))
    return flags


def total_for(scale: Scale, answers: Mapping[str, int]) -> int:
    """Балл по ответам (для risk_flags — вне скоринга, без повторной валидации)."""
    total = 0
    for item in scale.items:
        raw = answers.get(item.key, 0)
        total += item.max_value - raw if item.reverse else raw
    return total


__all__ = ["score", "risk_flags", "UnknownItemError", "IncompleteAnswersError"]