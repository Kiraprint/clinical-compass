# Контракт: docs/05-SDD.md §3 (risk_flags); docs/06-TDD.md §2.2
from compass.scales import PHQ9, GAD7
from compass.scoring import risk_flags


def test_suicidal_item_flags():
    answers = {f"q{i}": 0 for i in range(1, 9)} | {"q9": 1}
    flags = risk_flags(PHQ9, answers)
    assert any(f.kind == "SUICIDAL_IDEATION" for f in flags)
    assert flags[0].item_key == "q9"


def test_suicidal_zero_no_flag():
    assert risk_flags(PHQ9, {f"q{i}": 0 for i in range(1, 10)}) == []


def test_gad7_has_no_suicidal_flag():
    # В GAD-7 нет пункта о суицидальных мыслях
    assert risk_flags(GAD7, {f"q{i}": 3 for i in range(1, 8)}) == []