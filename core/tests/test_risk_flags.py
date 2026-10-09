# Контракт: docs/05-SDD.md §3 (risk_flags — обобщённые сигналы из структуры шкалы);
# docs/06-TDD.md §2.2. Флаги не привязаны к расстройству: SUICIDAL_IDEATION (scale.suicidal_item)
# и SEVERE_SYMPTOMS (шкала достигла собственного порога SEVERE).
import pytest

from compass.models import FlagKind, Severity
from compass.scales import PHQ9, GAD7, HADS_A, HADS_D
from compass.scoring import risk_flags, score


def phq(total: int, q9: int = 0) -> dict[str, int]:
    """Распределяет балл total по q1..q8 (макс 3), q9 задаётся явно."""
    answers, rem = {}, total
    for i in range(1, 9):
        take = min(3, rem)
        answers[f"q{i}"] = take
        rem -= take
    answers["q9"] = q9
    return answers


def gad(total: int) -> dict[str, int]:
    answers, rem = {}, total
    for i in range(1, 8):
        take = min(3, rem)
        answers[f"q{i}"] = take
        rem -= take
    return answers


def test_phq9_suicidal_item_flags():
    flags = risk_flags(PHQ9, phq(0, q9=1))
    kinds = {f.kind for f in flags}
    assert FlagKind.SUICIDAL_IDEATION in kinds
    assert all(f.kind == FlagKind.SUICIDAL_IDEATION and f.item_key == "q9" for f in flags if f.kind == FlagKind.SUICIDAL_IDEATION)


def test_phq9_green_no_flags():
    # total 0 (NONE), q9=0 → ни один флаг не эмитится
    assert risk_flags(PHQ9, phq(0)) == []


def test_phq9_severe_symptoms_flag_at_boundary():
    # total 15 → SEVERE_SYMPTOMS; 14 → нет (порог входит в SEVERE)
    assert any(f.kind == FlagKind.SEVERE_SYMPTOMS for f in risk_flags(PHQ9, phq(15)))
    assert not any(f.kind == FlagKind.SEVERE_SYMPTOMS for f in risk_flags(PHQ9, phq(14)))


def test_phq9_severe_symptoms_with_nonzero_q9():
    # total 15 + суицид → оба флага
    flags = risk_flags(PHQ9, phq(15, q9=1))
    kinds = {f.kind for f in flags}
    assert {FlagKind.SUICIDAL_IDEATION, FlagKind.SEVERE_SYMPTOMS} <= kinds


def test_gad7_no_suicidal_flag_but_severe_symptoms():
    # GAD-7 без суицидального пункта: SUICIDAL_IDEATION нет, SEVERE_SYMPTOMS при 15+
    flags = risk_flags(GAD7, gad(15))
    assert not any(f.kind == FlagKind.SUICIDAL_IDEATION for f in flags)
    assert any(f.kind == FlagKind.SEVERE_SYMPTOMS for f in flags)
    assert risk_flags(GAD7, gad(0)) == []


def test_hads_has_no_severe_bound_no_severe_flag():
    # HADS-A/HADS-D не достигают SEVERE (потолок MODERATE) → SEVERE_SYMPTOMS не эмитится
    def hads(scale: object, full: int) -> dict[str, int]:
        keys = [i.key for i in scale.items]
        answers, rem = {}, full
        for k in keys:
            take = min(3, rem)
            answers[k] = take
            rem -= take
        return answers
    assert not any(f.kind == FlagKind.SEVERE_SYMPTOMS for f in risk_flags(HADS_A, hads(HADS_A, 7 * 3)))
    assert not any(f.kind == FlagKind.SEVERE_SYMPTOMS for f in risk_flags(HADS_D, hads(HADS_D, 7 * 3)))


@pytest.mark.parametrize(
    "total,expected",
    [
        (0, Severity.NONE),
        (5, Severity.MILD),
        (10, Severity.MODERATE),
        (15, Severity.SEVERE),
    ],
)
def test_gad7_severity_boundaries(total, expected):
    assert score(GAD7, gad(total)).severity == expected
