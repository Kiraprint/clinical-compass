"""Тренды: expanding-скользящее среднее, дельты 7/30 дней, наклон (МНК).

Детерминированно; пустой вход → пустой тренд, без исключений.
"""

from __future__ import annotations

import statistics
from typing import Sequence

from compass.models import ScaleResult, Trend

_WINDOW = 7


def _slope(values: list[int]) -> float | None:
    if len(values) < 2:
        return None
    return statistics.linear_regression(range(len(values)), values).slope


def trends(runs: Sequence[ScaleResult]) -> Trend:
    scores = [r.total_score for r in runs]
    if not scores:
        return Trend(moving_avg=(), delta_7=None, delta_30=None, slope=None)

    # expanding-среднее с окном 7: i = 0..n-1, среднее за последние min(i+1, 7) точек
    avg: list[float] = []
    for i in range(len(scores)):
        window = scores[max(0, i - _WINDOW + 1) : i + 1]
        avg.append(sum(window) / len(window))

    def delta(n: int) -> float | None:
        if len(scores) > n:
            return float(scores[-1] - scores[-n - 1])
        return None

    return Trend(
        moving_avg=tuple(avg),
        delta_7=delta(7),
        delta_30=delta(30),
        slope=_slope(scores),
    )


__all__ = ["trends"]