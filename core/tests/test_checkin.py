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
    SAFETY_ITEMS,
    plan,
    planned_items,
    safety_level,
    select_modules,
    validate_answers,
)

CORE_CLEAN = {"MO1": 0, "TR1": 0, "SL1": 4, "IN1": 0, "EN1": 0, "FU1": 0, GATE_ITEM: 0}


def test_core_is_first_step():
    result = plan({})
    assert result.phase == "core"
    assert result.items == CORE_ITEMS
    assert len(CORE_ITEMS) == 7
    assert GATE_ITEM in CORE_ITEMS


def test_core_step_returns_only_unanswered_items():
    answers = {"MO1": 1, "TR1": 2}
    result = plan(answers)
    assert result.phase == "core"
    assert result.items == tuple(i for i in CORE_ITEMS if i not in answers)


def test_routing_is_deterministic():
    answers = {**CORE_CLEAN, "MO1": 3}
    assert plan(answers) == plan(answers)
    assert [d.code for d in select_modules(answers)] == [d.code for d in select_modules(answers)]


def test_clean_day_has_no_modules():
    result = plan(CORE_CLEAN)
    assert result.phase == "done"
    assert result.items == ()
    assert result.modules == ()
    assert result.safety == "none"
    assert len(planned_items(CORE_CLEAN)) == len(CORE_ITEMS)


def test_gate_yes_runs_safety_module_and_skips_symptoms():
    answers = {**CORE_CLEAN, "MO1": 4, GATE_ITEM: 1}
    result = plan(answers)
    assert result.phase == "safety"
    assert result.items == SAFETY_ITEMS
    assert result.safety == "review"
    # симптоматические модули в этот день не предъявляются
    assert result.modules == ()
    assert len(planned_items(answers)) == 10


def test_gate_yes_finishes_after_safety_module():
    answers = {**CORE_CLEAN, GATE_ITEM: 1, "RS1": 0, "RS2": 0, "RS3": 1}
    result = plan(answers)
    assert result.phase == "done"
    assert result.safety == "review"


@pytest.mark.parametrize(
    "answer",
    [{"RS1": 1}, {"RS2": 1}, {"RS3": 0}],
)
def test_urgent_signal(answer):
    answers = {**CORE_CLEAN, GATE_ITEM: 1, "RS1": 0, "RS2": 0, "RS3": 1, **answer}
    assert safety_level(answers) == "signal"


def test_gate_unanswered_is_unassessed_not_negative():
    answers = {k: v for k, v in CORE_CLEAN.items() if k != GATE_ITEM}
    assert safety_level(answers) == "unassessed"
    # ядро ждёт ответа на gate, а не уходит в симптоматику
    assert plan(answers).phase == "core"


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
    decisions = select_modules(heavy)
    included = [d for d in decisions if d.included]
    total = len(CORE_ITEMS) + sum(len(d.items) for d in included)
    assert total <= MAX_ITEMS
    # приоритет: HR (3) + SL (2) заполняют бюджет, остальные целиком пропущены
    assert [d.code for d in included] == ["HR", "SL"]
    assert all(not d.included and "не помещается" in d.reason for d in decisions[2:])
    ids = planned_items(heavy)
    assert len(ids) == MAX_ITEMS == len(set(ids))


def test_never_exceeds_limit_for_any_core_combination():
    values = (0, 2, 4)
    for combo in itertools.product(values, repeat=len(CORE_ITEMS)):
        answers: dict[str, int] = dict(zip(CORE_ITEMS, combo))
        if answers[GATE_ITEM] == 4:  # gate бинарный
            continue
        assert len(planned_items(answers)) <= MAX_ITEMS




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


def test_validate_answers_rejects_bad_values():
    validate_answers({"MO1": 4})
    for bad in ({"MO1": 5}, {"MO1": -1}, {"MO1": True}, {"NOPE": 1}):
        with pytest.raises(ValueError):
            validate_answers(bad)
