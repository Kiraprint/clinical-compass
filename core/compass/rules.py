"""Загрузка правил триажа/маршрутизации из конфига (метрика кейса 10).

Правила живут в `compass/data/alert_rules.json` — добавление правила (например,
«PHQ-9 q9>=2 → REQUIRED») не требует изменения кода ядра. Конфиг читается один
раз при импорте модуля (детерминированно, без сетевого I/O) и может быть
переопределён через переменную окружения `CC_ALERT_RULES` (путь к JSON) —
это позволяет подменить набор правил на инсталляции без пересборки пакета.

Ядро остаётся чистым: все функции ниже — чистые преобразования входного
состояния в решение.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_DEFAULT_PATH = Path(__file__).with_name("data") / "alert_rules.json"


@dataclass(frozen=True)
class RoutingRule:
    """Одно правило маршрутизации из конфига."""

    id: str
    category: str
    target: str
    reason: str
    flags_any: tuple[str, ...] = ()
    severities: tuple[str, ...] = ()
    triages: tuple[str, ...] = ()
    delta_7_gt: float | None = None
    match: str = "all"  # all | any


@dataclass(frozen=True)
class AlertRules:
    """Набор правил: триаж + упорядоченный список маршрутизации."""

    version: int
    red_flags_any: tuple[str, ...]
    red_severities: tuple[str, ...]
    yellow_severities: tuple[str, ...]
    green_fallback: str
    routing: tuple[RoutingRule, ...]
    routing_default: RoutingRule

    def matches(self, rule: RoutingRule, *, flags: tuple[str, ...], severity: str,
                triage_value: str, delta_7: float | None) -> bool:
        """Проверяет, применимо ли правило к состоянию пациента."""
        checks: list[bool] = []
        if rule.flags_any:
            checks.append(any(f in rule.flags_any for f in flags))
        if rule.severities:
            checks.append(severity in rule.severities)
        if rule.triages:
            checks.append(triage_value in rule.triages)
        if rule.delta_7_gt is not None:
            checks.append(delta_7 is not None and delta_7 > rule.delta_7_gt)
        if not checks:
            return False
        return any(checks) if rule.match == "any" else all(checks)


def _parse(raw: dict) -> AlertRules:
    triage_cfg = raw.get("triage", {})
    rules = tuple(
        RoutingRule(
            id=r.get("id", f"rule-{i}"),
            category=r["category"],
            target=r.get("target", "психиатр"),
            reason=r.get("reason", ""),
            flags_any=tuple(r.get("when", {}).get("flags_any", ())),
            severities=tuple(r.get("when", {}).get("severities", ())),
            triages=tuple(r.get("when", {}).get("triages", ())),
            delta_7_gt=r.get("when", {}).get("delta_7_gt"),
            match=r.get("match", "all"),
        )
        for i, r in enumerate(raw.get("routing", []))
    )
    d = raw.get("routing_default", {})
    return AlertRules(
        version=int(raw.get("version", 1)),
        red_flags_any=tuple(triage_cfg.get("red_flags_any", ())),
        red_severities=tuple(triage_cfg.get("red_severities", ())),
        yellow_severities=tuple(triage_cfg.get("yellow_severities", ())),
        green_fallback=triage_cfg.get("green_fallback", "GREEN"),
        routing=rules,
        routing_default=RoutingRule(
            id="default",
            category=d.get("category", "NONE"),
            target=d.get("target", "психиатр"),
            reason=d.get("reason", "динамическое наблюдение"),
        ),
    )


@lru_cache(maxsize=1)
def load_rules() -> AlertRules:
    """Читает правила из JSON (CC_ALERT_RULES или встроенный файл).

    Кешируется: ядро не делает I/O на каждый вызов. Для смены набора правил
    достаточно перезапустить процесс (или вызвать `load_rules.cache_clear()`).
    """
    override = os.environ.get("CC_ALERT_RULES")
    path = Path(override) if override else _DEFAULT_PATH
    with path.open(encoding="utf-8") as fh:
        return _parse(json.load(fh))


__all__ = ["AlertRules", "RoutingRule", "load_rules"]
