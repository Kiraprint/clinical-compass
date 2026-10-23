# Контракт: docs/04 §3.1; docs/06 §3 (consent_gating, screening_closes_run, screening_one_open_only)
from .conftest import admin_ctx, auth_header, clinician_ctx, login, patient_ctx, register

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


def test_questionnaires_expose_answer_anchors(client):
    """Каталог шкал: у каждого варианта ответа есть подпись (docs/04 §3.1)."""
    h, _ = patient_ctx(client, "anchors@test.ru")
    catalog = client.get("/api/v1/questionnaires", headers=h).json()
    assert catalog
    for scale in catalog:
        for item in scale["items"]:
            assert len(item["anchors"]) == item["max_value"] + 1, (scale["code"], item["key"])
            assert all(str(a).strip() for a in item["anchors"])
    phq = next(s for s in catalog if s["code"] == "PHQ-9")
    assert phq["items"][0]["anchors"] == [
        "Совсем нет", "Несколько дней", "Более половины дней", "Почти каждый день",
    ]
    hcl = next(s for s in catalog if s["code"] == "HCL-32")
    assert hcl["items"][0]["anchors"] == ["Нет", "Да"]



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
    """item 1: шкала вне назначенного множества → 403 SCALE_NOT_PRESCRIBED; без кода — первичная (дефолт PHQ-9)."""
    h, _ = patient_ctx(client, "cc@test.ru")
    # пациент без назначения имеет дефолт [PHQ-9]; GAD-7 не в множестве → 403
    resp = client.post("/api/v1/screenings", json={"questionnaire_code": "GAD-7"}, headers=h)
    assert resp.status_code == 403
    assert resp.json()["code"] == "SCALE_NOT_PRESCRIBED"
    # без кода — первичная назначенная (PHQ-9)
    resp = client.post("/api/v1/screenings", json={}, headers=h)
    assert resp.status_code == 201
    assert resp.json()["questionnaire_code"] == "PHQ-9"

def test_prescribed_scale_defaults_to_phq9(client):
    h, _ = patient_ctx(client, "def@test.ru")
    resp = client.get("/api/v1/me/prescribed-scales", headers=h)
    assert resp.status_code == 200
    assert [s["code"] for s in resp.json()["scales"]] == ["PHQ-9"]

def test_patient_can_start_any_prescribed_scale(client):
    """multi-scale: врач назначает [PHQ-9, GAD-7]; пациент стартует GAD-7, HADS-A — 403, без кода — PHQ-9."""
    h, pid = patient_ctx(client, "ms@test.ru")
    c, cid = clinician_ctx(client, "msc@test.ru")
    a, _ = admin_ctx(client, "msa@test.ru")
    # roster-грант от админа
    g = client.post("/api/v1/admin/roster", json={"patient_id": pid, "clinician_id": cid}, headers=a)
    assert g.status_code == 201, g.text
    resp = client.put(
        f"/api/v1/clinician/patients/{pid}/prescribed-scales",
        json={"scales": ["PHQ-9", "GAD-7"]}, headers=c,
    )
    assert resp.status_code == 200
    assert resp.json()["scales"] == ["PHQ-9", "GAD-7"]
    # пациент видит обе назначенные
    scales = client.get("/api/v1/me/prescribed-scales", headers=h).json()["scales"]
    assert [s["code"] for s in scales] == ["PHQ-9", "GAD-7"]
    # GAD-7 разрешён (в множестве)
    r = client.post("/api/v1/screenings", json={"questionnaire_code": "GAD-7"}, headers=h)
    assert r.status_code == 201 and r.json()["questionnaire_code"] == "GAD-7"
    client.post("/api/v1/screenings/abandon", headers=h)
    # HADS-A не назначена → 403
    r = client.post("/api/v1/screenings", json={"questionnaire_code": "HADS-A"}, headers=h)
    assert r.status_code == 403 and r.json()["code"] == "SCALE_NOT_PRESCRIBED"
    # без кода — первичная (PHQ-9, первая назначенная)
    r = client.post("/api/v1/screenings", json={}, headers=h)
    assert r.status_code == 201 and r.json()["questionnaire_code"] == "PHQ-9"

def test_questionnaires_exclude_clinician_scales(client):
    """HDRS-17 заполняет врач: в пациентском каталоге его нет, PSS-10 есть."""
    h, _ = patient_ctx(client, "catalog@test.ru")
    resp = client.get("/api/v1/questionnaires", headers=h)
    assert resp.status_code == 200
    codes = [s["code"] for s in resp.json()]
    assert "HDRS-17" not in codes
    assert "PSS-10" in codes

def test_patient_cannot_start_clinician_scale(client):
    h, _ = patient_ctx(client, "hdrsstart@test.ru")
    resp = client.post("/api/v1/screenings", json={"questionnaire_code": "HDRS-17"}, headers=h)
    assert resp.status_code == 403
    assert resp.json()["code"] == "SCALE_NOT_PATIENT_ADMINISTERED"
