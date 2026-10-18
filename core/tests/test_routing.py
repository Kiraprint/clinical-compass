# Контракт: docs/09 §P6; задача кейса 8 (маршрутизация обязательно/желательно).
from compass.models import RiskFlag, Severity, Triage
from compass.routing import RoutingCategory, routing_for


def _flags(*kinds: str) -> list[RiskFlag]:
    return [RiskFlag(kind=k) for k in kinds]


def test_suicidal_forces_required_regardless_of_severity():
    # Суицидальный флаг важнее severity — REQUIRED даже при низком балле.
    r = routing_for(Triage.RED, _flags("SUICIDAL_IDEATION"), Severity.NONE)
    assert r.category == RoutingCategory.REQUIRED
    assert r.target == "психиатр"
    assert r.label == "Обязательно"


def test_severe_without_suicidal_is_required():
    r = routing_for(Triage.RED, _flags("SEVERE_SYMPTOMS"), Severity.SEVERE)
    assert r.category == RoutingCategory.REQUIRED


def test_red_triage_is_required_even_if_severity_not_severe():
    # triage RED (напр. суицид уже учтён в triage) → REQUIRED.
    r = routing_for(Triage.RED, [], Severity.MODERATE)
    assert r.category == RoutingCategory.REQUIRED


def test_moderate_is_desirable():
    r = routing_for(Triage.YELLOW, [], Severity.MODERATE)
    assert r.category == RoutingCategory.DESIRABLE
    assert r.label == "Желательно"


def test_mild_with_worsening_is_desirable():
    # delta_7 > 0 = ухудшение (балл вырос за 7 дней).
    r = routing_for(Triage.YELLOW, [], Severity.MILD, delta_7=2.0)
    assert r.category == RoutingCategory.DESIRABLE


def test_mild_without_worsening_is_none():
    r = routing_for(Triage.YELLOW, [], Severity.MILD, delta_7=0.0)
    assert r.category == RoutingCategory.NONE


def test_none_severity_is_none():
    r = routing_for(Triage.GREEN, [], Severity.NONE)
    assert r.category == RoutingCategory.NONE
    assert r.label == "Не требуется"


def test_categories_visually_distinct_labels():
    # усл.6 кейса: «обязательно» и «желательно» различимы — разные строковые метки.
    req = routing_for(Triage.RED, _flags("SUICIDAL_IDEATION"), Severity.NONE).label
    des = routing_for(Triage.YELLOW, [], Severity.MODERATE).label
    none = routing_for(Triage.GREEN, [], Severity.NONE).label
    assert len({req, des, none}) == 3
