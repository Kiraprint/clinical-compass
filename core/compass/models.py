"""Модели клинического движка. Чистые dataclass без I/O. Контракт: docs/05-SDD.md §3."""

from __future__ import annotations

from dataclasses import dataclass

# Кто заполняет шкалу: пациент сам или только врач (HDRS-17 и т.п.).
AUDIENCE_PATIENT = "PATIENT"
AUDIENCE_CLINICIAN = "CLINICIAN"



class Severity:
    NONE = "NONE"
    MILD = "MILD"
    MODERATE = "MODERATE"
    SEVERE = "SEVERE"


class Triage:
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"


@dataclass(frozen=True)
class ScaleItem:
    key: str
    text: str
    max_value: int = 3
    reverse: bool = False  # HADS: обратные пункты (raw 0 → max балл)


@dataclass(frozen=True)
class Scale:
    code: str
    title: str
    items: tuple[ScaleItem, ...]
    severity_bounds: tuple[tuple[int, str], ...]  # сортировано по возрастанию (min, severity)
    suicidal_item: str | None = None
    audience: str = AUDIENCE_PATIENT

    def item_keys(self) -> frozenset[str]:
        return frozenset(i.key for i in self.items)

    def item(self, key: str) -> ScaleItem:
        for i in self.items:
            if i.key == key:
                return i
        raise KeyError(key)


    def severity_for(self, total: int) -> str:
        result: str | None = None
        for lo, sev in self.severity_bounds:
            if total >= lo:
                result = sev
            else:
                break
        return result or Severity.NONE


@dataclass(frozen=True)
class ScaleResult:
    code: str
    total_score: int
    severity: str
    per_item: dict[str, int]


class FlagKind:
    """Обобщённые виды риск-флагов. Выводятся из структуры шкалы,
    без привязки к конкретному расстройству (диагноз — функция врача)."""
    SUICIDAL_IDEATION = "SUICIDAL_IDEATION"  # любой scale.suicidal_item (пункт о суициде) >= 1
    SEVERE_SYMPTOMS = "SEVERE_SYMPTOMS"      # сумма баллов шкалы достигла собственного порога SEVERE


@dataclass(frozen=True)
class RiskFlag:
    kind: str
    item_key: str | None = None


@dataclass(frozen=True)
class Checkin:
    scale_code: str
    items: tuple[ScaleItem, ...]


@dataclass(frozen=True)
class Trend:
    moving_avg: tuple[float, ...]
    delta_7: float | None
    delta_30: float | None
    slope: float | None


__all__ = [
    "Severity",
    "AUDIENCE_PATIENT",
    "AUDIENCE_CLINICIAN",
    "Triage",
    "ScaleItem",
    "Scale",
    "ScaleResult",
    "RiskFlag",
    "FlagKind",
    "Checkin",
    "Trend",
]