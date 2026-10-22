"""FR-25: контракт мока ЕМИАС (docs/06 §3) — токен, scopes, Bundle, сбои."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.emias import mock

@pytest.fixture()
def mock_client(monkeypatch):
    monkeypatch.setenv("CC_EMIAS_MOCK_LATENCY_MS", "0")
    monkeypatch.setenv("CC_EMIAS_MOCK_TIMEOUT_S", "0")
    monkeypatch.delenv("CC_EMIAS_MOCK_RATE_LIMIT", raising=False)
    mock.reset_state()
    with TestClient(mock.app) as client:
        yield client
    mock.reset_state()

def _basic(client_id: str = mock.DEFAULT_CLIENT_ID, secret: str = mock.DEFAULT_CLIENT_SECRET) -> str:
    return "Basic " + base64.b64encode(f"{client_id}:{secret}".encode()).decode()

def _token(client, scope: str | None = None) -> str:
    data = {"grant_type": "client_credentials"}
    if scope is not None:
        data["scope"] = scope
    resp = client.post("/oauth/token", data=data, headers={"Authorization": _basic()})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]

def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}

def test_token_issues_bearer_and_expiry(mock_client):
    resp = mock_client.post(
        "/oauth/token",
        data={"grant_type": "client_credentials", "scope": "patient.read"},
        headers={"Authorization": _basic()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["access_token"].startswith("mock-")
    assert body["expires_in"] == mock.TOKEN_TTL
    assert body["scope"] == "patient.read"

def test_token_rejects_bad_credentials(mock_client):
    for headers in ({"Authorization": _basic(secret="nope")}, {}):
        resp = mock_client.post("/oauth/token", data={"grant_type": "client_credentials"}, headers=headers)
        assert resp.status_code == 401
        assert resp.json()["error"] == "invalid_client"

def test_token_requires_client_credentials_grant(mock_client):
    resp = mock_client.post("/oauth/token", data={"grant_type": "password"}, headers={"Authorization": _basic()})
    assert resp.status_code == 400
    assert resp.json()["error"] == "unsupported_grant_type"

def test_token_rate_limit(mock_client, monkeypatch):
    monkeypatch.setenv("CC_EMIAS_MOCK_RATE_LIMIT", "1")
    first = mock_client.post("/oauth/token", data={"grant_type": "client_credentials"}, headers={"Authorization": _basic()})
    assert first.status_code == 200
    second = mock_client.post("/oauth/token", data={"grant_type": "client_credentials"}, headers={"Authorization": _basic()})
    assert second.status_code == 429
    assert second.headers.get("Retry-After") == "1"

def test_patient_search_bundle_shape(mock_client):
    token = _token(mock_client)
    resp = mock_client.get("/fhir/Patient", params={"identifier": "11223344595"}, headers=_auth(token))
    assert resp.status_code == 200
    bundle = resp.json()
    assert bundle["resourceType"] == "Bundle"
    assert bundle["type"] == "searchset"
    assert bundle["total"] == 1
    patient = bundle["entry"][0]["resource"]
    assert patient["resourceType"] == "Patient"
    assert patient["id"] == "emias-1001"
    identifier = patient["identifier"][0]
    assert identifier["system"] == mock.SNILS_SYSTEM
    assert identifier["value"] == "112-233-445 95"
    assert patient["name"][0]["family"] == "Иванова"
    assert patient["name"][0]["given"] == ["Мария", "Петровна"]
    assert patient["telecom"][0]["system"] == "phone"
    assert patient["birthDate"] == "1985-04-12"
    assert patient["address"][0]["city"] == "Москва"

def test_patient_search_pagination_has_next_link(mock_client):
    token = _token(mock_client)
    resp = mock_client.get("/fhir/Patient", params={"_count": 1}, headers=_auth(token))
    bundle = resp.json()
    assert bundle["total"] == len(mock.PATIENTS)
    assert len(bundle["entry"]) == 1
    relations = {link["relation"]: link["url"] for link in bundle["link"]}
    assert "next" in relations and "_offset=1" in relations["next"]

    second = mock_client.get("/fhir/Patient", params={"_count": 1, "_offset": 1}, headers=_auth(token))
    assert second.json()["entry"][0]["resource"]["id"] != bundle["entry"][0]["resource"]["id"]

def test_patient_by_id_and_404(mock_client):
    token = _token(mock_client)
    assert mock_client.get("/fhir/Patient/emias-1002", headers=_auth(token)).json()["id"] == "emias-1002"
    missing = mock_client.get("/fhir/Patient/emias-9999", headers=_auth(token))
    assert missing.status_code == 404
    assert missing.json()["resourceType"] == "OperationOutcome"

def test_encounter_history(mock_client):
    token = _token(mock_client)
    resp = mock_client.get("/fhir/Encounter", params={"patient": "emias-1001"}, headers=_auth(token))
    assert resp.status_code == 200
    bundle = resp.json()
    assert bundle["total"] >= 2
    encounter = bundle["entry"][0]["resource"]
    assert encounter["resourceType"] == "Encounter"
    assert encounter["status"] == "finished"
    assert encounter["class"]["code"]
    assert encounter["period"]["start"]
    assert encounter["serviceProvider"]["display"]
    assert encounter["reasonCode"][0]["text"]
    assert encounter["participant"][0]["individual"]["display"]

def test_encounter_date_filter(mock_client):
    token = _token(mock_client)
    resp = mock_client.get(
        "/fhir/Encounter",
        params={"patient": "emias-1001", "date": "ge2026-01-01"},
        headers=_auth(token),
    )
    starts = [e["resource"]["period"]["start"] for e in resp.json()["entry"]]
    assert starts and all(start >= "2026-01-01" for start in starts)

def test_medication_history(mock_client):
    token = _token(mock_client)
    for patient_id in ("emias-1001", "emias-1002", "emias-1003"):
        bundle = mock_client.get(
            "/fhir/MedicationRequest", params={"patient": patient_id}, headers=_auth(token)
        ).json()
        assert bundle["total"] >= 2
        request = bundle["entry"][0]["resource"]
        assert request["medicationCodeableConcept"]["text"]
        assert request["dosageInstruction"][0]["text"]
        assert request["authoredOn"]
        assert request["status"]

def test_scope_enforcement(mock_client):
    token = _token(mock_client, scope="patient.read")
    # Patient доступен, Encounter/MedicationRequest — 403 insufficient_scope.
    assert mock_client.get("/fhir/Patient", headers=_auth(token)).status_code == 200
    for path, params in (("/fhir/Encounter", {"patient": "emias-1001"}),
                         ("/fhir/MedicationRequest", {"patient": "emias-1001"})):
        resp = mock_client.get(path, params=params, headers=_auth(token))
        assert resp.status_code == 403
        assert "insufficient_scope" in resp.json()["issue"][0]["diagnostics"]

def test_missing_token_is_401(mock_client):
    assert mock_client.get("/fhir/Patient").status_code == 401

def test_simulate_error_injection(mock_client):
    token = _token(mock_client)
    assert mock_client.get("/fhir/Patient", params={"simulate": "500"}, headers=_auth(token)).status_code == 500
    throttled = mock_client.get("/fhir/Patient", params={"simulate": "429"}, headers=_auth(token))
    assert throttled.status_code == 429
    assert throttled.headers.get("Retry-After") == "1"
    timed_out = mock_client.get("/fhir/Patient", params={"simulate": "timeout"}, headers=_auth(token))
    assert timed_out.status_code == 504

def test_health(mock_client):
    assert mock_client.get("/health").json()["status"] == "ok"
