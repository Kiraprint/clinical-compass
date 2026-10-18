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

    Правила (суицидальный флаг важнее severity):
    - SUICIDAL_IDEATION → REQUIRED (неотложно, независимо от балла);
    - SEVERE или triage RED → REQUIRED;
    - MODERATE → DESIRABLE;
    - MILD с ухудшением (delta_7 > 0) → DESIRABLE;
    - иначе → NONE (наблюдение).
    """
    if any(f.kind == "SUICIDAL_IDEATION" for f in risk_flags):
        return Routing(
            RoutingCategory.REQUIRED, "психиатр",
            "суицидальная идеация по шкале — неотложное направление",
        )
    if severity == Severity.SEVERE or triage_value == Triage.RED:
        return Routing(
            RoutingCategory.REQUIRED, "психиатр",
            "тяжёлая симптоматика (SEVERE/RED)",
        )
    if severity == Severity.MODERATE:
        return Routing(
            RoutingCategory.DESIRABLE, "психиатр",
            "симптоматика умеренной тяжести",
        )
    if severity == Severity.MILD and delta_7 is not None and delta_7 > 0:
        return Routing(
            RoutingCategory.DESIRABLE, "психиатр",
            "лёгкая симптоматика с отрицательной динамикой (ухудшение за 7 дней)",
        )
    return Routing(
        RoutingCategory.NONE, "психиатр",
        "направление не показано — динамическое наблюдение",
    )


__all__ = ["Routing", "RoutingCategory", "ROUTING_LABELS", "routing_for"]
