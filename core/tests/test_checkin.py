# Контракт: docs/05-SDD.md §3 (generate_checkin); docs/06-TDD.md §2.3
import pytest

from compass.models import ScaleResult, Severity
from compass.scales import PHQ9
from compass.checkin import generate_checkin


def last_with_worst(worst_key: str, value: int = 3) -> ScaleResult:
    per_item = {f"q{i}": 0 for i in range(1, 10)}
    per_item[worst_key] = value
    return ScaleResult(code="PHQ-9", total_score=value, severity=Severity.MILD, per_item=per_item)


def test_checkin_len_between_2_and_5():
    assert 2 <= len(generate_checkin(PHQ9).items) <= 5
    assert 2 <= len(generate_checkin(PHQ9, last_with_worst("q5")).items) <= 5

def test_checkin_can_reach_five_items():
    # 5 worst-пунктов + суицидальный → потолок MAX_ITEMS=5 достижим (не мёртвый код)
    per_item = {f"q{i}": 3 for i in range(1, 10)}
    last = ScaleResult(code="PHQ-9", total_score=27, severity=Severity.SEVERE, per_item=per_item)
    checkin = generate_checkin(PHQ9, last)
    assert len(checkin.items) == 5


def test_checkin_focuses_worst():
    # Worst-пункт последнего прохождения попадает в вопросы
    checkin = generate_checkin(PHQ9, last_with_worst("q5"))
    keys = {i.key for i in checkin.items}
    assert "q5" in keys


def test_checkin_all_zero_pads_to_min_two():
    # Пустое последнее прохождение → добираем пункты до минимума (2)
    checkin = generate_checkin(PHQ9, last_with_worst("q5", value=0))
    assert 2 <= len(checkin.items) <= 5


def test_checkin_items_bounded_by_scale():
    scale_keys = PHQ9.item_keys()
    for last in (None, last_with_worst("q1"), last_with_worst("q9")):
        checkin = generate_checkin(PHQ9, last)
        assert {i.key for i in checkin.items} <= scale_keys


def test_checkin_includes_suicidal_item_when_flagged():
    # Суицидальный пункт мониторится в чек-инах
    checkin = generate_checkin(PHQ9, last_with_worst("q9"))
    assert "q9" in {i.key for i in checkin.items}


@pytest.mark.parametrize("last", [None, last_with_worst("q2")])
def test_checkin_deterministic(last):
    # Не «f(x)==f(x)»: фиксированное ожидание — worst + суицидальный пункт.
    checkin = generate_checkin(PHQ9, last)
    keys = [i.key for i in checkin.items]
    if last is None:
        assert keys == ["q1", "q2", "q3"]  # нет данных → первые 3 пункта
    else:
        assert keys == ["q2", "q9"]  # worst q2 + суицидальный q9