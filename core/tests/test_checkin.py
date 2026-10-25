# Контракт: docs/16-ежедневный-чек-ин.md §1–§3; docs/06-TDD.md §2.3
import itertools

import pytest
from compass.checkin import (
    CORE_ITEMS,
    GATE_ITEM,
    ITEMS,
    MAX_ITEMS,
    MAX_VALUE,
    MODULES,
    RULES_VERSION,
    SAFETY_ITEMS,
    plan,
    planned_items,
    safety_gate,
    safety_gate_reason,
    safety_level,
    select_modules,
    validate_answers,
)

CORE_CLEAN = {"MO1": 0, "TR1": 0, "SL1": 4, "IN1": 0, "EN1": 0, "FU1": 0}


def test_core_is_first_step():
    result = plan({})
    assert result.phase == "core"
    assert result.items == CORE_ITEMS
    assert len(CORE_ITEMS) == 6
    assert GATE_ITEM not in CORE_ITEMS


def test_rules_version_is_2():
    assert RULES_VERSION == "2.0.0"
    assert MAX_ITEMS == 12


def test_core_step_returns_only_unanswered_items():
    answers = {"MO1": 1, "TR1": 2}
    result = plan(answers)
    assert result.phase == "core"
    assert result.items == tuple(i for i in CORE_ITEMS if i not in answers)


def test_routing_ignores_answer_order_and_repeats_identically():
    # Порядок ответов и порядок ключей в словаре не влияют на маршрут,
    # а приоритет правил даёт строго один и тот же список модулей.
    forward = {**CORE_CLEAN, "MO1": 4, "SL1": 0, "TR1": 3, "FU1": 3, GATE_ITEM: 0}
    backward = dict(reversed(list(forward.items())))
    assert plan(forward) == plan(backward)
    assert [d.code for d in select_modules(forward) if d.included] == ["HR", "SL"]
    assert plan({**forward, "HR1": 1, "HR2": 1, "HR3": 1}).items == ("SL2", "SL3")


def test_clean_day_skips_gate_and_is_unassessed():
    result = plan(CORE_CLEAN)
    assert result.phase == "done"
    assert result.items == ()
    assert result.modules == ()
    assert result.safety_gate is False
    # Нормальный терминальный статус: риск не оценивался (не «риска нет»).
    assert result.safety == "unassessed"
    assert planned_items(CORE_CLEAN) == CORE_ITEMS
    assert "не задаётся" in result.safety_gate_reason


@pytest.mark.parametrize(
    "trigger",
    [{"MO1": 3}, {"FU1": 3}, {"EN1": 2}],
)
def test_gate_triggers_after_core(trigger):
    answers = {**CORE_CLEAN, **trigger}
    assert safety_gate(answers) is True
    assert "HR4 задаётся" in safety_gate_reason(answers)
    result = plan(answers)
    assert result.phase == "safety"
    assert result.items == (GATE_ITEM,)
    assert result.safety_gate is True
    assert result.safety == "unassessed"
    assert len(planned_items(answers)) == len(CORE_ITEMS) + 1


def test_gate_is_not_decided_before_core_is_complete():
    answers = {"MO1": 4}
    assert safety_gate(answers) is False
    assert "ядро не заполнено" in safety_gate_reason(answers)
    assert plan(answers).phase == "core"


def test_gate_below_threshold_is_skipped():
    answers = {**CORE_CLEAN, "MO1": 2, "FU1": 2, "EN1": 1}
    assert safety_gate(answers) is False


def test_gate_off_still_routes_symptoms_with_unassessed_risk():
    answers = {**CORE_CLEAN, "MO1": 2}
    result = plan(answers)
    assert result.phase == "modules"
    assert result.safety_gate is False
    assert result.safety == "unassessed"
    assert "MO" in [d.code for d in result.modules if d.included]


def test_gate_no_continues_with_modules():
    answers = {**CORE_CLEAN, "MO1": 4, GATE_ITEM: 0}
    assert safety_gate(answers) is True
    result = plan(answers)
    assert result.phase == "modules"
    assert result.safety == "none"  # риск явно отрицается
    assert [d.code for d in result.modules if d.included][0] == "HR"


def test_gate_yes_runs_safety_module_and_skips_symptoms():
    answers = {**CORE_CLEAN, "MO1": 4, GATE_ITEM: 1}
    result = plan(answers)
    assert result.phase == "safety"
    assert result.items == SAFETY_ITEMS
    assert result.safety == "unassessed"  # RS ещё не пройден
    # симптоматические модули в этот день не предъявляются
    assert result.modules == ()
    assert len(planned_items(answers)) == 10


def test_gate_yes_finishes_after_safety_module():
    answers = {
        **CORE_CLEAN,
        "MO1": 4,
        GATE_ITEM: 1,
        "RS1": 0,
        "RS2": 0,
        "RS3": 1,
    }
    result = plan(answers)
    assert result.phase == "done"
    assert result.safety == "review"


@pytest.mark.parametrize(
    "answer",
    [{"RS1": 1}, {"RS2": 1}, {"RS3": 0}],
)
def test_urgent_signal(answer):
    answers = {
        **CORE_CLEAN,
        "MO1": 3,
        GATE_ITEM: 1,
        "RS1": 0,
        "RS2": 0,
        "RS3": 1,
        **answer,
    }
    assert safety_level(answers) == "signal"


def test_gate_unanswered_is_unassessed_not_negative():
    # Новое правило: ядро без HR4 завершается, но риск остаётся неоценённым.
    assert safety_level(CORE_CLEAN) == "unassessed"
    assert plan(CORE_CLEAN).safety == "unassessed"
    assert plan(CORE_CLEAN).phase == "done"


def test_positive_hr4_from_older_rules_still_runs_safety():
    # Сеанс, начатый по правилам 1.0.0, уже содержит HR4 = «Да»:
    # положительный ответ нельзя потерять, даже если gate v2 не сработал.
    answers = {**CORE_CLEAN, GATE_ITEM: 1}
    assert safety_gate(answers) is True
    result = plan(answers)
    assert result.phase == "safety"
    assert result.items == SAFETY_ITEMS


def test_thresholds_are_inclusive():
    assert [d.code for d in select_modules({**CORE_CLEAN, "MO1": 1})] == []
    assert "MO" in [d.code for d in select_modules({**CORE_CLEAN, "MO1": 2})]
    assert "FU" in [d.code for d in select_modules({**CORE_CLEAN, "FU1": 2})]
    assert "HR" in [d.code for d in select_modules({**CORE_CLEAN, "FU1": 3})]
    assert "SL" in [d.code for d in select_modules({**CORE_CLEAN, "SL1": 2})]
    assert "SL" not in [d.code for d in select_modules({**CORE_CLEAN, "SL1": 3})]
    assert "RU" in [d.code for d in select_modules({**CORE_CLEAN, "TR1": 2})]


def test_limit_is_twelve_and_modules_are_whole():
    heavy = {**CORE_CLEAN, "MO1": 4, "FU1": 4, "TR1": 4, "SL1": 0, "IN1": 4, "EN1": 4}
    decisions = select_modules({**heavy, GATE_ITEM: 0})
    included = [d for d in decisions if d.included]
    assert [d.code for d in included] == ["HR", "SL"]
    assert all(not d.included and "не помещается" in d.reason for d in decisions[2:])
    ids = planned_items({**heavy, GATE_ITEM: 0})
    assert len(ids) == MAX_ITEMS == len(set(ids))
    # gate-пункт тоже входит в лимит
    assert len(planned_items(heavy)) <= MAX_ITEMS


def test_never_exceeds_limit_for_any_core_combination():
    values = (0, 2, 4)
    for combo in itertools.product(values, repeat=len(CORE_ITEMS)):
        answers: dict[str, int] = dict(zip(CORE_ITEMS, combo))
        assert len(planned_items(answers)) <= MAX_ITEMS
        if safety_gate(answers):
            assert len(planned_items({**answers, GATE_ITEM: 0})) <= MAX_ITEMS
            assert len(planned_items({**answers, GATE_ITEM: 1})) <= MAX_ITEMS


def test_module_items_are_unique_and_cover_bank():
    ids = [i.id for i in ITEMS]
    assert len(ids) == len(set(ids))
    for module, _ in MODULES:
        assert len(set(module.items)) == len(module.items)
        for item_id in module.items:
            assert item_id in MAX_VALUE


def test_anchors_match_scale_width():
    for i in ITEMS:
        assert len(i.anchors) == len(i.emoji)
        assert len(i.anchors) == (5 if i.kind == "scale" else 2)
        if i.recall == "last_night":
            assert i.domain == "sleep"


def test_risk_item_wording_is_person_safe():
    gate = next(i for i in ITEMS if i.id == GATE_ITEM)
    assert gate.text == "Были ли у вас мысли о самоповреждении?"
    assert gate.anchors == ("Нет", "Да")


def test_validate_answers_rejects_bad_values():
    validate_answers({"MO1": 4})
    for bad in ({"MO1": 5}, {"MO1": -1}, {"MO1": True}, {"NOPE": 1}):
        with pytest.raises(ValueError):
            validate_answers(bad)
