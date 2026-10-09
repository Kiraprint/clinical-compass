# Контракт: docs/05-SDD.md §3 (triage); docs/06-TDD.md §2.2
from compass.models import Triage
from compass.scales import PHQ9
from compass.scoring import score, risk_flags
from compass.triage import triage


def fill(total: int, q9: int = 0) -> dict[str, int]:
    answers, rem = {}, total
    for i in range(1, 9):
        take = min(3, rem)
        answers[f"q{i}"] = take
        rem -= take
    answers["q9"] = q9
    return answers


def triage_of(total: int, q9: int = 0) -> str:
    answers = fill(total, q9)
    return triage(score(PHQ9, answers), risk_flags(PHQ9, answers))


def test_triage_red_suicidal():
    # Суицидальный флаг важнее severity: MILD + флаг → RED
    assert triage_of(1, q9=1) == Triage.RED


def test_triage_red_severe():
    assert triage_of(15) == Triage.RED


def test_triage_yellow():
    assert triage_of(12) == Triage.YELLOW  # MODERATE
    assert triage_of(5) == Triage.YELLOW   # MILD


def test_triage_green():
    assert triage_of(0) == Triage.GREEN


def test_triage_deterministic():
    assert triage_of(9) == triage_of(9)