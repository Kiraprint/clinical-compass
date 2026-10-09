# Контракт: docs/04 §4; docs/06 §3 (escalation_under_1s, escalation_notifies)
import time

from .conftest import patient_ctx

PHQ_ALL = {f"q{i}": 0 for i in range(1, 10)}


def _finish(client, h, answers) -> dict:
    run_id = client.post("/api/v1/screenings", json={"questionnaire_code": "PHQ-9"}, headers=h).json()["run_id"]
    assert client.post(f"/api/v1/runs/{run_id}/answers", json={"answers": answers}, headers=h).status_code == 204
    resp = client.post(f"/api/v1/runs/{run_id}/finish", headers=h)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_suicidal_answer_escalates(client):
    h, _ = patient_ctx(client, "suic@test.ru")
    assessment = _finish(client, h, dict(PHQ_ALL, q9=1))
    assert assessment["triage"] == "RED"
    assert assessment["risk_flags"][0]["kind"] == "SUICIDAL_IDEATION"
    assert assessment["escalation"]["kind"] == "SUICIDAL"
    assert assessment["escalation"]["help_resources"]


def test_severe_score_escalates(client):
    h, _ = patient_ctx(client, "sev@test.ru")
    # total 15: q1..q5 = 3 → RED без суицидального флага
    answers = {**PHQ_ALL, **{f"q{i}": 3 for i in range(1, 6)}}
    assessment = _finish(client, h, answers)
    assert assessment["triage"] == "RED"
    assert assessment["escalation"]["kind"] == "SEVERE"


def test_escalation_response_under_1s(client):
    h, _ = patient_ctx(client, "fast@test.ru")
    start = time.perf_counter()
    _finish(client, h, dict(PHQ_ALL, q9=1))
    assert time.perf_counter() - start < 1.0


def test_no_escalation_for_green(client):
    h, _ = patient_ctx(client, "green@test.ru")
    assessment = _finish(client, h, PHQ_ALL)
    assert assessment["triage"] == "GREEN"
    assert "escalation" not in assessment