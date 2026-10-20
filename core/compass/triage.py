"""Триаж: GREEN/YELLOW/RED. Правила — data-driven (метрика кейса 10).

Источник правил — `compass/data/alert_rules.json` (см. `compass.rules`).
По умолчанию: RED — суицидальный флаг ИЛИ SEVERE (флаг важнее severity);
YELLOW — MILD/MODERATE без красных условий; GREEN — остальное.
"""

from __future__ import annotations

from typing import Sequence

from compass.models import RiskFlag, ScaleResult, Severity, Triage
from compass.rules import load_rules


def triage(result: ScaleResult, flags: Sequence[RiskFlag]) -> str:
    rules = load_rules()
    flag_kinds = {f.kind for f in flags}
    if flag_kinds & set(rules.red_flags_any):
        return Triage.RED
    if result.severity in rules.red_severities:
        return Triage.RED
    if result.severity in rules.yellow_severities:
        return Triage.YELLOW
    return rules.green_fallback


__all__ = ["triage"]
