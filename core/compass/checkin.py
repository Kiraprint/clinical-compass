"""Генерация ежедневных чек-инов. Детерминированно, без рандома.

Правило: нет данных — первые 3 пункта шкалы; есть предыдущее прохождение —
worst-пункты (балл > 0, по убыванию) + суицидальный пункт, добив до минимума 2.
"""

from __future__ import annotations

from compass.models import Checkin, Scale, ScaleResult

MIN_ITEMS = 2
MAX_ITEMS = 5


def generate_checkin(scale: Scale, last: ScaleResult | None = None) -> Checkin:
    if last is None or last.code != scale.code:
        chosen = list(scale.items[:3])
    else:
        ordered = [
            scale.item(key)
            for key, _ in sorted(last.per_item.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        chosen = [item for item in ordered if last.per_item[item.key] > 0][: MAX_ITEMS - 1]
        if scale.suicidal_item:
            suicidal = scale.item(scale.suicidal_item)
            if suicidal not in chosen:
                chosen.append(suicidal)  # суицидальный пункт мониторится всегда
        if len(chosen) < MIN_ITEMS:
            for item in scale.items:
                if len(chosen) >= MIN_ITEMS:
                    break
                if item not in chosen:
                    chosen.append(item)
    return Checkin(scale_code=scale.code, items=tuple(chosen[:MAX_ITEMS]))


__all__ = ["generate_checkin", "MIN_ITEMS", "MAX_ITEMS"]