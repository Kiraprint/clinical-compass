# Контракт: docs/04 §3.1; docs/06 §3 (consent_gating, screening_closes_run, screening_one_open_only)
from .conftest import auth_header, login, patient_ctx, register

PHQ_ALL = {f"q{i}": 0 for i in range(1, 10)}


def test_screening_requires_consent(client):
    register(client, "nocon@test.ru")
    data = login(client, "nocon@test.ru")
    resp = client.post(
        "/api/v1/screenings", json={}, headers=auth_header(data["access_token"])
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "CONSENT_REQUIRED"


def test_screening_flow_phq9(client):
    h, _ = patient_ctx(client, "flow@test.ru")
    resp = client.post("/api/v1/screenings", json={}, headers=h)
    assert resp.status_code == 201
    run_id = resp.json()["run_id"]

    answers = dict(PHQ_ALL, q1=1, q2=1, q3=1, q4=1, q5=1)  # total 5 → MILD
    assert client.post(f"/api/v1/runs/{run_id}/answers", json={"answers": answers}, headers=h).status_code == 204

    resp = client.post(f"/api/v1/runs/{run_id}/finish", headers=h)
    assert resp.status_code == 201, resp.text
    assessment = resp.json()
    assert assessment["total_score"] == 5
    assert assessment["severity"] == "MILD"
    assert assessment["triage"] == "YELLOW"


def test_second_open_screening_conflict(client):
    h, _ = patient_ctx(client, "two@test.ru")
    assert client.post("/api/v1/screenings", json={}, headers=h).status_code == 201
    resp = client.post("/api/v1/screenings", json={}, headers=h)
    assert resp.status_code == 409
    assert resp.json()["code"] == "CONFLICT"


def test_run_closed_after_finish(client):
    h, _ = patient_ctx(client, "closed@test.ru")
    run_id = client.post("/api/v1/screenings", json={}, headers=h).json()["run_id"]
    client.post(f"/api/v1/runs/{run_id}/answers", json={"answers": PHQ_ALL}, headers=h)
    assert client.post(f"/api/v1/runs/{run_id}/finish", headers=h).status_code == 201
    resp = client.post(f"/api/v1/runs/{run_id}/answers", json={"answers": {"q1": 1}}, headers=h)
    assert resp.status_code == 409
    assert resp.json()["code"] == "RUN_CLOSED"


def test_answers_unknown_item_422(client):
    h, _ = patient_ctx(client, "baditem@test.ru")
    run_id = client.post("/api/v1/screenings", json={}, headers=h).json()["run_id"]
    resp = client.post(f"/api/v1/runs/{run_id}/answers", json={"answers": {"zzz": 1}}, headers=h)
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION"


def test_finish_incomplete_422(client):
    h, _ = patient_ctx(client, "inc@test.ru")
    run_id = client.post("/api/v1/screenings", json={}, headers=h).json()["run_id"]
    resp = client.post(
        f"/api/v1/runs/{run_id}/answers", json={"answers": {f"q{i}": 0 for i in range(1, 9)}}, headers=h
    )
    assert resp.status_code == 204
    resp = client.post(f"/api/v1/runs/{run_id}/finish", headers=h)
    assert resp.status_code == 422

def test_abandon_open_screening_allows_restart(client):
    h, _ = patient_ctx(client, "abandon@test.ru")
    assert client.post("/api/v1/screenings", json={}, headers=h).status_code == 201
    # брошенный открытый скрининг не даёт начать новый → abandon → снова ок
    resp = client.post("/api/v1/screenings", json={}, headers=h)
    assert resp.status_code == 409
    cresp = client.post("/api/v1/screenings/abandon", headers=h)
    assert cresp.status_code == 200
    assert cresp.json()["abandoned"] is True
    assert client.post("/api/v1/screenings", json={}, headers=h).status_code == 201

def test_abandon_no_open_run_is_noop(client):
    h, _ = patient_ctx(client, "abandon2@test.ru")
    resp = client.post("/api/v1/screenings/abandon", headers=h)
    assert resp.status_code == 200
    assert resp.json()["abandoned"] is False

def test_finish_out_of_range_422(client):
    h, _ = patient_ctx(client, "oor@test.ru")
    run_id = client.post("/api/v1/screenings", json={}, headers=h).json()["run_id"]
    resp = client.post(
        f"/api/v1/runs/{run_id}/answers", json={"answers": {**PHQ_ALL, "q1": 4}}, headers=h
    )
    assert resp.status_code == 422  # _validate_answers: значение вне 0..max_value
    assert resp.json()["code"] == "VALIDATION"

def test_patient_cannot_choose_scale(client):
    """item 1: шкала назначается врачом — клиентский questionnaire_code игнорируется (дефолт PHQ-9)."""
    h, _ = patient_ctx(client, "nochoice@test.ru")
    # даже если клиент шлёт произвольный код — сервер использует дефолт/предписание
    resp = client.post("/api/v1/screenings", json={"questionnaire_code": "GAD-7"}, headers=h)
    assert resp.status_code == 201
    assert resp.json()["questionnaire_code"] == "PHQ-9"

def test_prescribed_scale_defaults_to_phq9(client):
    h, _ = patient_ctx(client, "def@test.ru")
    resp = client.get("/api/v1/me/prescribed-scale", headers=h)
    assert resp.status_code == 200
    assert resp.json()["questionnaire_code"] == "PHQ-9"
