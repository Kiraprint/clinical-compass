"""P11 (метрика кейса 10): правила триажа/маршрутизации data-driven.

Добавление правила не требует изменения кода ядра — только конфига
`compass/data/alert_rules.json` (или CC_ALERT_RULES). Эти тесты доказывают,
что конфиг реально управляет поведением.
"""

from __future__ import annotations

import json

import pytest

from compass.models import RiskFlag, ScaleResult, Severity, Triage
from compass.routing import RoutingCategory, routing_for
from compass.rules import load_rules
from compass.triage import triage


@pytest.fixture
def custom_rules(tmp_path, monkeypatch):
    """Подменяет конфиг правил на пользовательский и сбрасывает кеш."""
    def _apply(raw: dict):
        path = tmp_path / "rules.json"
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setenv("CC_ALERT_RULES", str(path))
        load_rules.cache_clear()
        return path

    yield _apply
    monkeypatch.delenv("CC_ALERT_RULES", raising=False)
    load_rules.cache_clear()


def _result(severity: str) -> ScaleResult:
    return ScaleResult(code="PHQ-9", total_score=10, severity=severity, per_item={})


def test_default_rules_load_from_json():
    """Встроенный конфиг читается и содержит правила кейса."""
    rules = load_rules()
    assert rules.version >= 1
    assert "SUICIDAL_IDEATION" in rules.red_flags_any
    assert [r.id for r in rules.routing][0] == "suicidal-ideation"


def test_new_triage_threshold_from_config(custom_rules):
    """Правило «MILD тоже RED» добавляется конфигом, без правки triage()."""
    custom_rules({
        "version": 1,
        "triage": {
            "red_flags_any": ["SUICIDAL_IDEATION"],
            "red_severities": ["SEVERE", "MILD"],
            "yellow_severities": ["MODERATE"],
            "green_fallback": "GREEN",
        },
        "routing": [],
        "routing_default": {"category": "NONE", "target": "психиатр", "reason": "—"},
    })
    assert triage(_result(Severity.MILD), []) == Triage.RED
    assert triage(_result(Severity.MODERATE), []) == Triage.YELLOW


def test_new_routing_rule_from_config(custom_rules):
    """Новое правило маршрутизации (delta_7) добавляется конфигом."""
    custom_rules({
        "version": 1,
        "triage": {"red_flags_any": [], "red_severities": ["SEVERE"],
                   "yellow_severities": ["MILD", "MODERATE"], "green_fallback": "GREEN"},
        "routing": [
            {"id": "worsening-any", "when": {"delta_7_gt": 2},
             "category": "REQUIRED", "target": "психиатр",
             "reason": "ухудшение более чем на 2 балла за неделю"},
        ],
        "routing_default": {"category": "NONE", "target": "психиатр", "reason": "—"},
    })
    r = routing_for(Triage.GREEN, [], Severity.NONE, delta_7=3)
    assert r.category == RoutingCategory.REQUIRED
    assert "2 балла" in r.reason


def test_routing_rule_priority_follows_config_order(custom_rules):
    """Порядок правил в конфиге задаёт приоритет (первое совпавшее выигрывает)."""
    custom_rules({
        "version": 1,
        "triage": {"red_flags_any": [], "red_severities": ["SEVERE"],
                   "yellow_severities": ["MILD", "MODERATE"], "green_fallback": "GREEN"},
        "routing": [
            {"id": "first", "when": {"severities": ["SEVERE"]},
             "category": "DESIRABLE", "target": "психиатр", "reason": "первое правило"},
            {"id": "second", "when": {"severities": ["SEVERE"]},
             "category": "REQUIRED", "target": "психиатр", "reason": "второе правило"},
        ],
        "routing_default": {"category": "NONE", "target": "психиатр", "reason": "—"},
    })
    r = routing_for(Triage.RED, [], Severity.SEVERE)
    assert r.category == RoutingCategory.DESIRABLE
    assert r.reason == "первое правило"


def test_default_routing_behavior_unchanged():
    """Дефолтный конфиг сохраняет клиническое поведение из docs/09."""
    suicidal = [RiskFlag(kind="SUICIDAL_IDEATION", item_key="q9")]
    assert routing_for(Triage.RED, suicidal, Severity.NONE).category == RoutingCategory.REQUIRED
    assert routing_for(Triage.RED, [], Severity.SEVERE).category == RoutingCategory.REQUIRED
    assert routing_for(Triage.YELLOW, [], Severity.MODERATE).category == RoutingCategory.DESIRABLE
    assert routing_for(Triage.YELLOW, [], Severity.MILD, delta_7=1.5).category == RoutingCategory.DESIRABLE
    assert routing_for(Triage.GREEN, [], Severity.NONE).category == RoutingCategory.NONE
