"""Триаж: GREEN/YELLOW/RED. Детерминированное правило из severity + риск-флагов.

RED: суицидальный флаг ИЛИ SEVERE (флаг важнее severity).
YELLOW: MILD/MODERATE без красных условий. GREEN: NONE без флагов.
"""

from __future__ import annotations

from typing import Sequence

from compass.models import RiskFlag, ScaleResult, Severity, Triage


def triage(result: ScaleResult, flags: Sequence[RiskFlag]) -> str:
    if any(f.kind == "SUICIDAL_IDEATION" for f in flags) or result.severity == Severity.SEVERE:
        return Triage.RED
    if result.severity in (Severity.MILD, Severity.MODERATE):
        return Triage.YELLOW
    return Triage.GREEN


__all__ = ["triage"]