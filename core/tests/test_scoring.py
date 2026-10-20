# Контракт: docs/05-SDD.md §3 (scoring, risk_flags); пороги — docs/06-TDD.md §2.1-2.2
import pytest

from compass.models import Severity
from compass.scales import PHQ9, HADS_D
from compass.scoring import score, UnknownItemError, IncompleteAnswersError


def phq_all() -> dict[str, int]:
    return {f"q{i}": 0 for i in range(1, 10)}


def fill(total: int, q9: int = 0) -> dict[str, int]:
    """Распределяет балл total по q1..q8 (макс 3), q9 задаётся явно."""
    answers, rem = {}, total
    for i in range(1, 9):
        take = min(3, rem)
        answers[f"q{i}"] = take
        rem -= take
    answers["q9"] = q9
    return answers


def test_score_sums_items():
    res = score(PHQ9, phq_all() | {"q1": 2, "q2": 1})
    assert res.total_score == 3
    assert res.per_item["q1"] == 2
    assert res.per_item["q2"] == 1
    assert all(v == 0 for k, v in res.per_item.items() if k not in ("q1", "q2"))


def test_score_unknown_item_raises():
    with pytest.raises(UnknownItemError):
        score(PHQ9, phq_all() | {"zzz": 1})


def test_score_missing_items_raises():
    answers = phq_all()
    del answers["q1"]
    with pytest.raises(IncompleteAnswersError):
        score(PHQ9, answers)


def test_score_value_out_of_range():
    with pytest.raises(ValueError):
        score(PHQ9, phq_all() | {"q1": 4})


@pytest.mark.parametrize(
    "total,expected",
    [
        (0, Severity.NONE),
        (4, Severity.NONE),
        (5, Severity.MILD),
        (9, Severity.MILD),
        (10, Severity.MODERATE),
        (14, Severity.MODERATE),
        (15, Severity.SEVERE),
        (27, Severity.SEVERE),
    ],
)
def test_phq9_severity_boundaries(total, expected):
    assert score(PHQ9, fill(total)).severity == expected


def test_hads_d_reverse_scoring():
    # Обратные пункты HADS-D (d1,d2,d3,d5,d6,d7): raw 0 → 3 балла; прямой d4: raw 0 → 0
    res = score(HADS_D, {f"d{i}": 0 for i in range(1, 8)})
    assert res.total_score == 18
    assert res.per_item["d4"] == 0
    assert res.per_item["d1"] == 3
    # Все raw 3 → баллы только за прямой пункт
    res2 = score(HADS_D, {f"d{i}": 3 for i in range(1, 8)})
    assert res2.total_score == 3
    assert res2.per_item["d4"] == 3