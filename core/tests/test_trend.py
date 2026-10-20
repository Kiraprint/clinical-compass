# Контракт: docs/05-SDD.md §3 (trends); docs/06-TDD.md §2.4
from compass.models import ScaleResult, Severity
from compass.trend import trends


def series(values: list[int]) -> list[ScaleResult]:
    return [
        ScaleResult(code="PHQ-9", total_score=v, severity=Severity.MILD, per_item={})
        for v in values
    ]


def test_trend_empty_input():
    t = trends([])
    assert t.moving_avg == ()
    assert t.delta_7 is None
    assert t.delta_30 is None
    assert t.slope is None


def test_trend_moving_avg_same_length():
    # Окно 7 > длины ряда: expanding-среднее, длина не меняется
    t = trends(series([1, 2, 3, 4, 5]))
    assert len(t.moving_avg) == 5
    assert t.moving_avg[-1] == 3.0


def test_trend_delta_30():
    # 31 точка, прирост 1/день: delta_30 = конец − точка 30 назад = 30
    t = trends(series(list(range(31))))
    assert t.delta_30 == 30
    assert t.delta_7 == 7  # 30 − 23


def test_trend_slope_linear():
    t = trends(series([0, 1, 2, 3, 4]))
    assert t.slope is not None and abs(t.slope - 1.0) < 1e-9


def test_trend_short_series_no_deltas():
    t = trends(series([1, 2]))
    assert t.delta_30 is None
    assert t.delta_7 is None