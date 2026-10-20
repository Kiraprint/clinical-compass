"""Маршрутизация пациента к психиатру (задача кейса 8).

Детерминированная рекомендация направления к психиатру — для врачей других
специальностей. Категории «обязательно»/«желательно» различимы (обязательное
условие 6 кейса). Решение — рекомендация, не действие: направление происходит
только после подтверждения врачом (обязательное условие 3).

Границы — [ДОПУЩЕНИЕ — подтвердить медиком команды].
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from compass.models import RiskFlag, Severity, Triage
from compass.rules import load_rules


class RoutingCategory:
    REQUIRED = "REQUIRED"      # обязательно
    DESIRABLE = "DESIRABLE"    # желательно
    NONE = "NONE"              # не требуется


# Русские метки для UI (категории визуально различимы — усл.6).
ROUTING_LABELS = {
    RoutingCategory.REQUIRED: "Обязательно",
    RoutingCategory.DESIRABLE: "Желательно",
    RoutingCategory.NONE: "Не требуется",
}


@dataclass(frozen=True)
class Routing:
    category: str
    target: str          # «психиатр»
    reason: str

    @property
    def label(self) -> str:
        return ROUTING_LABELS.get(self.category, self.category)


def routing_for(
    triage_value: str,
    risk_flags: Sequence[RiskFlag],
    severity: str,
    delta_7: float | None = None,
) -> Routing:
    """Возвращает рекомендацию маршрутизации к психиатру.

    Правила читаются из `compass/data/alert_rules.json` (метрика кейса 10):
    порядок правил в конфиге определяет приоритет, первое совпавшее выигрывает.
    По умолчанию: суицидальный флаг → REQUIRED; SEVERE/RED → REQUIRED;
    MODERATE → DESIRABLE; MILD с ухудшением (delta_7 > 0) → DESIRABLE; иначе NONE.
    """
    rules = load_rules()
    flag_kinds = tuple(f.kind for f in risk_flags)
    for rule in rules.routing:
        if rules.matches(
            rule,
            flags=flag_kinds,
            severity=severity,
            triage_value=triage_value,
            delta_7=delta_7,
        ):
            return Routing(rule.category, rule.target, rule.reason)
    d = rules.routing_default
    return Routing(d.category, d.target, d.reason)


__all__ = ["Routing", "RoutingCategory", "ROUTING_LABELS", "routing_for"]
