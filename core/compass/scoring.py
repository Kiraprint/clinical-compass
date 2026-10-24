"""Скоринг и риск-флаги. Чистые функции; ошибки — подклассы ValueError (API → 422)."""

from __future__ import annotations

from typing import Iterable, Mapping

from compass.models import FlagKind, RiskFlag, Scale, ScaleResult, Severity


class UnknownItemError(ValueError):
    """Ответ содержит пункт, отсутствующий в шкале."""


class IncompleteAnswersError(ValueError):
    """Не все пункты шкалы заполнены."""


class HiddenItemAnsweredError(ValueError):
    """Пришёл ответ на пункт, который пациенту не предъявляется."""


def _check_omitted(scale: Scale, omitted: Iterable[str]) -> frozenset[str]:
    """Пропускать можно только пункты, скрытые от пациента (q9 самоопросника).

    Клиницистские шкалы заполняются целиком: молчаливое «пункт не задавался»
    у HDRS-17 скрыло бы риск, поэтому это ошибка, а не допустимый пропуск.
    """
    keys = frozenset(omitted)
    for key in keys:
        scale.item(key)  # KeyError → ValueError ниже, если пункта нет
        if scale.item(key).patient_visible:
            raise ValueError(f"item {key!r} of scale {scale.code} cannot be omitted")
    return keys


def score(scale: Scale, answers: Mapping[str, int], *, omitted: Iterable[str] = ()) -> ScaleResult:
    """Считает балл шкалы. Детерминированно; error-first валидация.

    Инварианты: total_score == sum по предъявленным items; обратные пункты (HADS)
    переворачиваются. `omitted` — пункты, скрытые от пациента (не задавались):
    они не входят ни в сумму, ни в per_item, а их ключи возвращаются в
    ScaleResult.omitted_items (пометка «шкала неполная» для отчёта и UI).
    """
    try:
        skipped = _check_omitted(scale, omitted)
    except KeyError as exc:
        raise UnknownItemError(f"unknown omitted item {exc.args[0]!r} for scale {scale.code}") from None
    keys = scale.item_keys()
    for key in answers:
        if key not in keys:
            raise UnknownItemError(f"unknown item {key!r} for scale {scale.code}")
        if key in skipped:
            raise HiddenItemAnsweredError(f"item {key!r} is not asked to the patient")
    expected = len(scale.items) - len(skipped)
    if len(answers) != expected:
        raise IncompleteAnswersError(f"expected {expected} answers, got {len(answers)}")

    per_item: dict[str, int] = {}
    total = 0
    for item in scale.items:
        if item.key in skipped:
            continue
        raw = answers[item.key]
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise ValueError(f"value {raw!r} for item {item.key} is not an int (ожидается 0..{item.max_value})")  # → 422
        if raw < 0 or raw > item.max_value:
            raise ValueError(f"value {raw} out of range 0..{item.max_value} for item {item.key}")
        value = item.max_value - raw if item.reverse else raw
        per_item[item.key] = value
        total += value
    return ScaleResult(
        code=scale.code,
        total_score=total,
        severity=scale.severity_for(total),
        per_item=per_item,
        omitted_items=tuple(i.key for i in scale.items if i.key in skipped),
    )


def risk_flags(
    scale: Scale, answers: Mapping[str, int], *, omitted: Iterable[str] = ()
) -> list[RiskFlag]:
    """Риск-флаги, выводимые из структуры шкалы — без привязки к расстройству:

    - SUICIDAL_IDEATION: scale.suicidal_item (пункт о суициде) >= 1
    - SEVERE_SYMPTOMS: сумма баллов достигла собственного порога SEVERE шкалы
    - NOT_ASSESSED: пункт о суициде не задавался → риск НЕ оценён (не «нет»)
    """
    skipped = frozenset(omitted)
    flags: list[RiskFlag] = []
    if scale.suicidal_item:
        if scale.suicidal_item in skipped:
            flags.append(RiskFlag(kind=FlagKind.NOT_ASSESSED, item_key=scale.suicidal_item))
        elif answers.get(scale.suicidal_item, 0) >= 1:
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
