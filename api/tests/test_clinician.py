# E5: панель врача, roster, аудит доступа. Контракт: docs/04 §3.1; docs/06 §3.
from .conftest import admin_ctx, auth_header, clinician_ctx, finish_screening, patient_ctx

PHQ_ALL = {f"q{i}": 0 for i in range(1, 10)}


def _with_patient(client, email: str, answers: dict | None = None) -> tuple[dict, str]:
    h, pid = patient_ctx(client, email)
    finish_screening(client, h, answers or PHQ_ALL)
    return h, pid


def _grant(client, admin_h, patient_id, clinician_id) -> None:
    resp = client.post(
        "/api/v1/admin/roster", json={"patient_id": patient_id, "clinician_id": clinician_id}, headers=admin_h
    )
    assert resp.status_code == 201, resp.text


def test_clinician_cannot_see_unrostered(client):
    _, pid = _with_patient(client, "pu@test.ru")
    c, _ = clinician_ctx(client, "cu@test.ru")
    resp = client.get(f"/api/v1/clinician/patients/{pid}/report", headers=c)
    assert resp.status_code == 403
    assert resp.json()["code"] == "NO_ROSTER"


def test_report_after_roster_and_audit(client):
    _, pid = _with_patient(client, "pr@test.ru")
    c, cid = clinician_ctx(client, "cr@test.ru")
    a, _ = admin_ctx(client, "ar@test.ru")
    _grant(client, a, pid, cid)

    resp = client.get(f"/api/v1/clinician/patients/{pid}/report", headers=c)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["last_assessment"]["total_score"] == 0
    assert body["last_assessment"]["triage"] == "GREEN"

    # Аудит чтения отчёта
    audit = client.get("/api/v1/admin/audit", headers=a).json()["items"]
    assert any(e["action"] == "READ_REPORT" and e["object_id"] == str(pid) for e in audit)


def test_clinician_patients_list_and_triage_filter(client):
    _, pid = _with_patient(client, "pl@test.ru")  # GREEN (0)
    c, cid = clinician_ctx(client, "cl@test.ru")
    a, _ = admin_ctx(client, "al@test.ru")
    _grant(client, a, pid, cid)

    cards = client.get("/api/v1/clinician/patients", headers=c).json()["items"]
    assert any(card["patient_id"] == str(pid) and card["last_triage"] == "GREEN" for card in cards)

    red_cards = client.get("/api/v1/clinician/patients?triage=RED", headers=c).json()["items"]
    assert red_cards == []


def test_patient_cannot_access_clinician_routes(client):
    h, pid = _with_patient(client, "pp@test.ru")
    resp = client.get("/api/v1/clinician/patients", headers=h)
    assert resp.status_code == 403
    assert resp.json()["code"] == "ROLE_MISMATCH"
    resp = client.get(f"/api/v1/clinician/patients/{pid}/report", headers=h)
    assert resp.status_code == 403


def test_foreign_run_hidden(client):
    h_a, _ = _with_patient(client, "own@test.ru")
    h_b, _ = patient_ctx(client, "other@test.ru")
    run_a = client.post("/api/v1/screenings", json={}, headers=h_a).json()["run_id"]
    # B пытается завершить/дополнить run пациента A → 404 (владелец только A, _own_run)
    resp = client.post(f"/api/v1/runs/{run_a}/answers", json={"answers": PHQ_ALL}, headers=h_b)
    assert resp.status_code == 404
    resp = client.post(f"/api/v1/runs/{run_a}/finish", headers=h_b)
    assert resp.status_code == 404


def test_diary_in_report(client):
    h, pid = _with_patient(client, "dr@test.ru")
    assert client.post(
        "/api/v1/diary", json={"content": "Сегодня получше", "mood": 7, "sleep_h": 6.5, "tags": ["сон"]}, headers=h
    ).status_code == 201
    c, cid = clinician_ctx(client, "dc@test.ru")
    a, _ = admin_ctx(client, "da@test.ru")
    _grant(client, a, pid, cid)
    report = client.get(f"/api/v1/clinician/patients/{pid}/report", headers=c).json()
    assert report["diary"][0]["content"] == "Сегодня получше"
def test_prescribe_scale_roster_gated(client):
    """item 1: врач назначает шкалу; без roster — 403 NO_ROSTER."""
    _, pid = _with_patient(client, "ps@test.ru")
    c, _ = clinician_ctx(client, "pc@test.ru")
    resp = client.put(f"/api/v1/clinician/patients/{pid}/prescribed-scale", json={"scale": "PHQ-9"}, headers=c)
    assert resp.status_code == 403
    assert resp.json()["code"] == "NO_ROSTER"

def test_prescribe_scale_applies_to_patient(client):
    """item 1: после назначения пациент скринится на выбранной врачом шкале."""
    _, pid = _with_patient(client, "ps2@test.ru")
    c, cid = clinician_ctx(client, "pc2@test.ru")
    a, _ = admin_ctx(client, "pa2@test.ru")
    _grant(client, a, pid, cid)

    resp = client.put(f"/api/v1/clinician/patients/{pid}/prescribed-scale", json={"scale": "GAD-7"}, headers=c)
    assert resp.status_code == 200
    assert resp.json()["questionnaire_code"] == "GAD-7"

    # Пациент теперь видит назначенную шкалу
    h = client.post(
        "/api/v1/auth/login", json={"email": "ps2@test.ru", "password": "Str0ng-Pass!"}
    ).json()
    # ps2 зарегистрирован через patient_ctx с дефолтным паролем Str0ng-Pass!
    hdr = auth_header(h["access_token"])
    ps = client.get("/api/v1/me/prescribed-scale", headers=hdr)
    assert ps.status_code == 200
    assert ps.json()["questionnaire_code"] == "GAD-7"

    # Отчёт врача отражает назначение
    report = client.get(f"/api/v1/clinician/patients/{pid}/report", headers=c)
    assert report.json()["prescribed_scale"] == "GAD-7"
