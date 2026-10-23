"""Канонические шкалы: PSS-10, HDRS-17 и аудитория заполнения."""

import pytest

from compass.models import AUDIENCE_CLINICIAN, AUDIENCE_PATIENT, ScaleItem, Severity, Triage
from compass.scales import (
    ALL_SCALES,
    HCL32,
    HDRS17,
    PHQ9,
    PSS10,
    clinician_administered_scales,
    patient_administered_scales,
)
from compass.scoring import risk_flags, score
from compass.triage import triage


def pss_fill(total: int) -> dict[str, int]:
    """Распределяет total по 10 пунктам PSS-10 (0..4), обратные — перевёрнуты."""
    answers: dict[str, int] = {}
    rem = total
    for item in PSS10.items:
        take = min(4, rem)
        rem -= take
        answers[item.key] = item.max_value - take if item.reverse else take
    assert rem == 0, f"PSS-10 maximum is 40, got {total}"
    return answers


def hdrs_fill(total: int) -> dict[str, int]:
    """Распределяет total по пунктам HDRS-17 с их максимумами (0..4 / 0..2)."""
    answers: dict[str, int] = {}
    rem = total
    for item in HDRS17.items:
        take = min(item.max_value, rem)
        rem -= take
        answers[item.key] = take
    assert rem == 0, f"HDRS-17 maximum is 52, got {total}"
    return answers


def test_pss10_is_canonical():
    assert len(PSS10.items) == 10
    assert all(i.max_value == 4 for i in PSS10.items)
    assert {i.key for i in PSS10.items if i.reverse} == {"p4", "p5", "p7", "p8"}
    # 10 пунктов * 4 = 40; тексты не дублируются.
    assert len({i.text for i in PSS10.items}) == 10


def test_pss10_reverse_scoring_concrete_total():
    answers = {
        "p1": 0, "p2": 1, "p3": 2, "p4": 0, "p5": 1,
        "p6": 2, "p7": 3, "p8": 4, "p9": 0, "p10": 1,
    }
    res = score(PSS10, answers)
    # 0 + 1 + 2 + (4-0) + (4-1) + 2 + (4-3) + (4-4) + 0 + 1 = 14
    assert res.total_score == 14
    assert res.severity == Severity.MILD
    assert res.per_item["p4"] == 4
    assert res.per_item["p8"] == 0


def test_pss10_all_zero_is_maximum_stress():
    res = score(PSS10, {i.key: 0 for i in PSS10.items})
    assert res.total_score == 16  # четыре обратных пункта дают по 4
    assert res.severity == Severity.MILD

def test_pss10_all_four_is_mild():
    res = score(PSS10, {i.key: 4 for i in PSS10.items})
    assert res.total_score == 24  # прямые пункты 6*4, обратные — 0
    assert res.severity == Severity.MILD


def test_pss10_severity_boundaries():
    assert score(PSS10, pss_fill(0)).severity == Severity.NONE
    assert score(PSS10, pss_fill(13)).severity == Severity.NONE
    assert score(PSS10, pss_fill(14)).severity == Severity.MILD
    assert score(PSS10, pss_fill(26)).severity == Severity.MILD
    assert score(PSS10, pss_fill(27)).severity == Severity.MODERATE
    assert score(PSS10, pss_fill(40)).severity == Severity.MODERATE


def test_hdrs17_severity_boundaries():
    expected = [
        (0, Severity.NONE), (7, Severity.NONE),
        (8, Severity.MILD), (16, Severity.MILD),
        (17, Severity.MODERATE), (23, Severity.MODERATE),
        (24, Severity.SEVERE), (52, Severity.SEVERE),
    ]
    for total, severity in expected:
        assert score(HDRS17, hdrs_fill(total)).severity == severity, total

def test_hdrs17_triage():
    mild = hdrs_fill(8)
    mild["h3"] = 0
    res = score(HDRS17, mild)
    assert res.severity == Severity.MILD
    assert triage(res, risk_flags(HDRS17, mild)) == Triage.YELLOW

    severe = {i.key: 0 for i in HDRS17.items}
    for key in ("h1", "h2", "h7", "h8", "h9", "h10"):
        severe[key] = 4  # сумма 24, суицидальный пункт h3 = 0
    res = score(HDRS17, severe)
    assert res.severity == Severity.SEVERE
    assert triage(res, risk_flags(HDRS17, severe)) == Triage.RED

    suicidal = hdrs_fill(0) | {"h3": 1}
    res = score(HDRS17, suicidal)
    flags = risk_flags(HDRS17, suicidal)
    assert any(f.kind == "SUICIDAL_IDEATION" for f in flags)
    assert triage(res, flags) == Triage.RED


def test_audience_filter_helpers():
    patient_codes = [s.code for s in patient_administered_scales()]
    clinician_codes = [s.code for s in clinician_administered_scales()]
    assert "HDRS-17" not in patient_codes
    assert "PSS-10" in patient_codes
    assert clinician_codes == ["HDRS-17"]
    assert HDRS17.audience == AUDIENCE_CLINICIAN
    assert PSS10.audience == AUDIENCE_PATIENT


def test_every_item_has_anchors_for_all_values():
    """У каждого варианта ответа есть подпись: len(anchors) == max_value + 1."""
    for scale in ALL_SCALES.values():
        for item in scale.items:
            assert len(item.anchors) == item.max_value + 1, (scale.code, item.key)
            assert all(a.strip() for a in item.anchors), (scale.code, item.key)


def test_binary_scale_anchors_are_yes_no():
    assert HCL32.items and all(i.max_value == 1 for i in HCL32.items)
    assert all(i.anchors == ("Нет", "Да") for i in HCL32.items)


def test_questionnaire_frequency_anchors():
    """PHQ-9/GAD-7 — стандартные подписи «за последние 2 недели»."""
    expected = ("Совсем нет", "Несколько дней", "Более половины дней", "Почти каждый день")
    assert all(i.anchors == expected for i in PHQ9.items)
    # PSS-10 — «за последний месяц», 5 вариантов.
    assert all(
        i.anchors == ("Никогда", "Почти никогда", "Иногда", "Довольно часто", "Очень часто")
        for i in PSS10.items
    )


def test_hdrs17_anchors_follow_item_max_value():
    """HDRS-17: клинические якоря по каждому пункту, число вариантов = max_value + 1."""
    assert [len(i.anchors) for i in HDRS17.items] == [5, 5, 5, 3, 3, 3, 5, 5, 5, 5, 5, 3, 3, 3, 5, 3, 3]
    assert HDRS17.item("h3").anchors[0] == "Нет"
    assert len(set(HDRS17.item("h1").anchors)) == 5


def test_anchors_length_mismatch_raises():
    with pytest.raises(ValueError):
        ScaleItem("x", "текст", max_value=4, anchors=("a", "b"))
    # Пустые anchors — допустимо (пункт без подписей).
    assert ScaleItem("y", "текст", max_value=4).anchors == ()
