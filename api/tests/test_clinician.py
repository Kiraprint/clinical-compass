# E5: панель врача, roster, аудит доступа. Контракт: docs/04 §3.1; docs/06 §3.
from app.models import AuditLog

from .conftest import admin_ctx, auth_header, clinician_ctx, finish_screening, patient_ctx

PHQ_ALL = {f"q{i}": 0 for i in range(1, 10)}

HDRS_ALL_ZERO = {f"h{i}": 0 for i in range(1, 18)}


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


def test_report_routing_required_for_suicidal(client):
    # PHQ-9 q9>=1 → SUICIDAL_IDEATION → RED → маршрутизация ОБЯЗАТЕЛЬНО.
    suicidal = {**PHQ_ALL, "q9": 2}
    _, pid = _with_patient(client, "psuic@test.ru", suicidal)
    c, cid = clinician_ctx(client, "crs@test.ru")
    a, _ = admin_ctx(client, "ars@test.ru")
    _grant(client, a, pid, cid)
    body = client.get(f"/api/v1/clinician/patients/{pid}/report", headers=c).json()
    assert body["last_assessment"]["triage"] == "RED"
    assert body["routing"]["category"] == "REQUIRED"
    assert body["routing"]["label"] == "Обязательно"

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
    # Маршрутизация (задача кейса 8): GREEN/NONE без флагов → не требуется.
    assert body["routing"]["category"] == "NONE"
    assert body["routing"]["target"] == "психиатр"

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
        "/api/v1/diary", json={"content": "Сегодня получше", "mood": 3, "sleep_h": 6.5, "tags": ["сон"]}, headers=h
    ).status_code == 201
    c, cid = clinician_ctx(client, "dc@test.ru")
    a, _ = admin_ctx(client, "da@test.ru")
    _grant(client, a, pid, cid)
    report = client.get(f"/api/v1/clinician/patients/{pid}/report", headers=c).json()
    assert report["diary"][0]["content"] == "Сегодня получше"
def test_prescribe_scale_roster_gated(client):
    """item 1: врач назначает шкалы; без roster — 403 NO_ROSTER."""
    _, pid = _with_patient(client, "ps@test.ru")
    c, _ = clinician_ctx(client, "pc@test.ru")
    resp = client.put(f"/api/v1/clinician/patients/{pid}/prescribed-scales", json={"scales": ["PHQ-9"]}, headers=c)
    assert resp.status_code == 403
    assert resp.json()["code"] == "NO_ROSTER"

def test_prescribe_scale_applies_to_patient(client):
    """item 1: после назначения пациент скринится на выбранной врачом шкале."""
    _, pid = _with_patient(client, "ps2@test.ru")
    c, cid = clinician_ctx(client, "pc2@test.ru")
    a, _ = admin_ctx(client, "pa2@test.ru")
    _grant(client, a, pid, cid)

    resp = client.put(f"/api/v1/clinician/patients/{pid}/prescribed-scales", json={"scales": ["GAD-7"]}, headers=c)
    assert resp.status_code == 200
    assert resp.json()["scales"] == ["GAD-7"]

    # Пациент теперь видит назначенную шкалу
    h = client.post(
        "/api/v1/auth/login", json={"email": "ps2@test.ru", "password": "Str0ng-Pass!"}
    ).json()
    # ps2 зарегистрирован через patient_ctx с дефолтным паролем Str0ng-Pass!
    hdr = auth_header(h["access_token"])
    ps = client.get("/api/v1/me/prescribed-scales", headers=hdr)
    assert ps.status_code == 200
    assert [s["code"] for s in ps.json()["scales"]] == ["GAD-7"]

    # Отчёт врача отражает назначение
    report = client.get(f"/api/v1/clinician/patients/{pid}/report", headers=c)
    assert report.json()["prescribed_scales"] == ["GAD-7"]


def test_adverse_event_create_list_and_audit(client):
    """задача кейса 3: регистрация НЯ, roster-гейт, аудит ADVERSE_EVENT."""
    _, pid = _with_patient(client, "ae@test.ru")
    c, cid = clinician_ctx(client, "aec@test.ru")
    a, _ = admin_ctx(client, "aea@test.ru")
    _grant(client, a, pid, cid)

    body = {
        "event_text": "Головная боль после начала терапии",
        "severity": "mild",
        "onset_date": "2026-10-18",
        "suspected_drug": "Авиандр",
        "action_taken": "monitoring",
    }
    resp = client.post(f"/api/v1/clinician/patients/{pid}/adverse-events", json=body, headers=c)
    assert resp.status_code == 201, resp.text
    ev = resp.json()
    assert ev["severity"] == "mild" and ev["suspected_drug"] == "Авиандр"

    items = client.get(f"/api/v1/clinician/patients/{pid}/adverse-events", headers=c).json()["items"]
    assert len(items) == 1 and items[0]["id"] == ev["id"]

    audit = client.get("/api/v1/admin/audit", headers=a).json()["items"]
    assert any(e["action"] == "ADVERSE_EVENT" and e["object_id"] == str(pid) for e in audit)


def test_adverse_event_roster_gated(client):
    _, pid = _with_patient(client, "ae2@test.ru")
    c, _ = clinician_ctx(client, "aec2@test.ru")
    body = {"event_text": "x", "severity": "mild", "onset_date": "2026-10-18"}
    resp = client.post(f"/api/v1/clinician/patients/{pid}/adverse-events", json=body, headers=c)
    assert resp.status_code == 403
    assert resp.json()["code"] == "NO_ROSTER"


def test_adherence_patient_records_and_doctor_aggregate(client):
    """задача кейса 7: пациент отмечает приверженность, врач видит агрегат % в карточке."""
    h, pid = _with_patient(client, "ad@test.ru")
    # Пациент отмечает 3 записи: 2 taken + 1 missed → pct = (2 + 0)/3*100 ≈ 66.7
    for st in ("taken", "taken", "missed"):
        r = client.post("/api/v1/me/adherence", json={"medication": "Авиандр", "status": st}, headers=h)
        assert r.status_code == 201, r.text

    c, cid = clinician_ctx(client, "adc@test.ru")
    a, _ = admin_ctx(client, "ada@test.ru")
    _grant(client, a, pid, cid)

    summ = client.get(f"/api/v1/clinician/patients/{pid}/adherence", headers=c).json()
    assert summ["total"] == 3 and summ["taken"] == 2 and summ["missed"] == 1
    assert summ["pct"] == 66.7

    # Агрегат есть и в отчёте
    report = client.get(f"/api/v1/clinician/patients/{pid}/report", headers=c).json()
    assert report["adherence"]["pct"] == 66.7
    assert report["adherence"]["records"][0]["medication"] == "Авиандр"


def test_patient_reads_own_adherence_and_read_is_audited(client, db_session):
    """Экран «Сегодня»: пациент читает свои отметки; GET /me/adherence пишет READ_ADHERENCE в аудит."""
    h, pid = _with_patient(client, "adr@test.ru")
    r = client.post("/api/v1/me/adherence", json={"medication": "Авиандр", "status": "partial"}, headers=h)
    assert r.status_code == 201, r.text

    got = client.get("/api/v1/me/adherence", headers=h)
    assert got.status_code == 200, got.text
    body = got.json()
    assert len(body["items"]) == 1 and body["items"][0]["status"] == "partial"
    assert body["today"] is not None and body["today"]["status"] == "partial"

    db_session.expire_all()
    actions = [a.action for a in db_session.query(AuditLog).filter(AuditLog.actor_id == pid).all()]
    assert "READ_ADHERENCE" in actions

def test_clinician_chat_patient_sees_message(client):
    """задача кейса 6: врач пишет пациенту, пациент видит сообщение в своём чате."""
    h, pid = _with_patient(client, "ch@test.ru")
    c, cid = clinician_ctx(client, "chc@test.ru")
    a, _ = admin_ctx(client, "cha@test.ru")
    _grant(client, a, pid, cid)

    r = client.post(f"/api/v1/clinician/patients/{pid}/chat",
                    json={"content": "Как вы себя чувствуете после начала терапии?"}, headers=c)
    assert r.status_code == 201, r.text
    assert r.json()["sender"] == "CLINICIAN"

    # Пациент видит сообщение врача в своём чате
    thread = client.get("/api/v1/chat/thread", headers=h).json()["items"]
    assert any(m["sender"] == "CLINICIAN" and "после начала терапии" in m["content"] for m in thread)

    # Аудит
    audit = client.get("/api/v1/admin/audit", headers=a).json()["items"]
    assert any(e["action"] == "CLINICIAN_CHAT" and e["object_id"] == str(pid) for e in audit)


def test_clinician_chat_roster_gated(client):
    _, pid = _with_patient(client, "ch2@test.ru")
    c, _ = clinician_ctx(client, "chc2@test.ru")
    r = client.post(f"/api/v1/clinician/patients/{pid}/chat", json={"content": "x"}, headers=c)
    assert r.status_code == 403
    assert r.json()["code"] == "NO_ROSTER"


TEMPLATE_A = """Жалобы: постоянная тревога, нарушение сна, снижение настроения
Анамнез: симптомы около 6 месяцев, ранее не обращался
Диагноз: F41.1 Генерализованное тревожное расстройство
Лечение: Авиандр 20 мг 2 раза в сутки
PHQ-9: 12, GAD-7 = 14"""

# Тот же клинический смысл, другой порядок разделов и стиль фиксации.
TEMPLATE_B = """Диагноз — F41.1
Назначено: Авиандр 20 мг
Жалобы: тревога, бессонница
GAD-7: 14
Анамнез заболевания: 6 месяцев"""


def test_import_history_parses_different_templates(client):
    """метрика кейса 2: извлечение из историй разных шаблонов (порядок/стиль/формат)."""
    _, pid = _with_patient(client, "hi@test.ru")
    c, cid = clinician_ctx(client, "hic@test.ru")
    a, _ = admin_ctx(client, "hia@test.ru")
    _grant(client, a, pid, cid)

    ra = client.post(f"/api/v1/clinician/patients/{pid}/import-history",
                     json={"text": TEMPLATE_A, "template_name": "A"}, headers=c)
    assert ra.status_code == 200, ra.text
    a_body = ra.json()
    assert "F41.1" in a_body["diagnosis_codes"]
    assert any("Авиандр" in t for t in a_body["therapy"])
    assert a_body["complaints"] and "тревога" in a_body["complaints"]
    assert a_body["requires_confirmation"] is True

    rb = client.post(f"/api/v1/clinician/patients/{pid}/import-history",
                     json={"text": TEMPLATE_B, "template_name": "B"}, headers=c)
    assert rb.status_code == 200, rb.text
    b_body = rb.json()
    # Тот же диагноз и препарат извлечены несмотря на другой порядок/стиль.
    assert "F41.1" in b_body["diagnosis_codes"]
    assert any("Авиандр" in t for t in b_body["therapy"])
    assert any("GAD-7" in s for s in b_body["scale_scores"])

    audit = client.get("/api/v1/admin/audit", headers=a).json()["items"]
    assert sum(1 for e in audit if e["action"] == "IMPORT_HISTORY") == 2


def test_import_history_roster_gated(client):
    _, pid = _with_patient(client, "hi2@test.ru")
    c, _ = clinician_ctx(client, "hic2@test.ru")
    r = client.post(f"/api/v1/clinician/patients/{pid}/import-history",
                    json={"text": TEMPLATE_A}, headers=c)
    assert r.status_code == 403
    assert r.json()["code"] == "NO_ROSTER"

def test_visit_create_list_and_audit(client):
    """задача кейса 6, п. «приём»: запись беседы, история, аудит VISIT."""
    _, pid = _with_patient(client, "visit@test.ru")
    c, cid = clinician_ctx(client, "visitc@test.ru")
    a, _ = admin_ctx(client, "visita@test.ru")
    _grant(client, a, pid, cid)

    body = {
        "visited_at": "2026-10-18T12:00:00+03:00",
        "visit_type": "online",
        "duration_min": 50,
        "notes": "Обсудили динамику тревоги, план — продолжить терапию.",
    }
    resp = client.post(f"/api/v1/clinician/patients/{pid}/visits", json=body, headers=c)
    assert resp.status_code == 201, resp.text
    v = resp.json()
    assert v["visit_type"] == "online" and v["duration_min"] == 50
    assert v["notes"].startswith("Обсудили динамику")

    items = client.get(f"/api/v1/clinician/patients/{pid}/visits", headers=c).json()["items"]
    assert len(items) == 1 and items[0]["id"] == v["id"]

    audit = client.get("/api/v1/admin/audit", headers=a).json()["items"]
    assert any(e["action"] == "VISIT" and e["object_id"] == str(pid) for e in audit)

def test_visit_delete_removes_and_audits(client):
    """Ошибочную запись приёма можно удалить; удаление аудируется."""
    _, pid = _with_patient(client, "visit3@test.ru")
    c, cid = clinician_ctx(client, "visitc3@test.ru")
    a, _ = admin_ctx(client, "visita3@test.ru")
    _grant(client, a, pid, cid)

    r = client.post(f"/api/v1/clinician/patients/{pid}/visits", headers=c, json={
        "visited_at": "2026-10-18T12:00:00+03:00", "visit_type": "phone", "notes": "Опечатка",
    })
    assert r.status_code == 201, r.text
    vid = r.json()["id"]

    d = client.delete(f"/api/v1/clinician/patients/{pid}/visits/{vid}", headers=c)
    assert d.status_code == 204, d.text
    assert client.get(f"/api/v1/clinician/patients/{pid}/visits", headers=c).json()["items"] == []

    audit = client.get("/api/v1/admin/audit", headers=a).json()["items"]
    assert any(e["action"] == "VISIT_DELETE" and e["object_id"] == str(pid) for e in audit)

    assert client.delete(f"/api/v1/clinician/patients/{pid}/visits/{vid}", headers=c).status_code == 404

def test_visit_notes_encrypted_at_rest(client, db_session):
    """152-ФЗ: заметка приёма не лежит в БД открытым текстом."""
    _, pid = _with_patient(client, "visit2@test.ru")
    c, cid = clinician_ctx(client, "visitc2@test.ru")
    a, _ = admin_ctx(client, "visita2@test.ru")
    _grant(client, a, pid, cid)

    secret = "Пациент упомянул конфликт на работе"
    r = client.post(f"/api/v1/clinician/patients/{pid}/visits", headers=c, json={
        "visited_at": "2026-10-18T12:00:00+03:00", "visit_type": "in_person",
        "notes": secret,
    })
    assert r.status_code == 201, r.text

    from sqlalchemy import text as sql_text
    raw = db_session.execute(sql_text("select notes from visits")).scalar_one()
    assert isinstance(raw, (bytes, bytearray, memoryview))
    assert secret.encode() not in bytes(raw)

def test_visit_roster_gated(client):
    """Roster-гейт: чужой пациент недоступен (403 NO_ROSTER)."""
    _, pid = _with_patient(client, "visit3@test.ru")
    c, _ = clinician_ctx(client, "visitc3@test.ru")
    r = client.post(f"/api/v1/clinician/patients/{pid}/visits", headers=c, json={
        "visited_at": "2026-10-18T12:00:00+03:00", "visit_type": "phone", "notes": "x",
    })
    assert r.status_code == 403
    assert r.json()["code"] == "NO_ROSTER"

def test_visit_rejects_bad_type_and_duration(client):
    """Валидация: недопустимый формат/длительность — 422."""
    _, pid = _with_patient(client, "visit4@test.ru")
    c, cid = clinician_ctx(client, "visitc4@test.ru")
    a, _ = admin_ctx(client, "visita4@test.ru")
    _grant(client, a, pid, cid)

    bad_type = client.post(f"/api/v1/clinician/patients/{pid}/visits", headers=c, json={
        "visited_at": "2026-10-18T12:00:00+03:00", "visit_type": "carrier_pigeon", "notes": "x",
    })
    assert bad_type.status_code == 422
    bad_dur = client.post(f"/api/v1/clinician/patients/{pid}/visits", headers=c, json={
        "visited_at": "2026-10-18T12:00:00+03:00", "visit_type": "online",
        "duration_min": 0, "notes": "x",
    })
    assert bad_dur.status_code == 422

def test_prescribe_clinician_only_scale_422(client):
    """HDRS-17 заполняет врач: пациенту назначить нельзя (422)."""
    _, pid = _with_patient(client, "phdrs@test.ru")
    c, cid = clinician_ctx(client, "chdrs@test.ru")
    a, _ = admin_ctx(client, "ahdrs@test.ru")
    _grant(client, a, pid, cid)

    resp = client.put(
        f"/api/v1/clinician/patients/{pid}/prescribed-scales",
        json={"scales": ["HDRS-17"]}, headers=c,
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "SCALE_NOT_PATIENT_ADMINISTERED"
    report = client.get(f"/api/v1/clinician/patients/{pid}/report", headers=c).json()
    assert report["prescribed_scales"] == []

def test_clinician_assessment_red_and_history(client):
    """Врачебная HDRS-17: Run+Assessment+RED-эскалация, аудит и история отчёта."""
    _, pid = _with_patient(client, "cassess@test.ru")
    c, cid = clinician_ctx(client, "cassessc@test.ru")
    a, _ = admin_ctx(client, "cassessa@test.ru")
    _grant(client, a, pid, cid)

    answers = {**HDRS_ALL_ZERO, "h3": 1}
    resp = client.post(
        f"/api/v1/clinician/patients/{pid}/assessments",
        json={"questionnaire_code": "HDRS-17", "answers": answers}, headers=c,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["questionnaire_code"] == "HDRS-17"
    assert body["total_score"] == 1
    assert body["severity"] == "NONE"
    assert body["triage"] == "RED"
    assert body["escalation"]["kind"] == "SUICIDAL"
    assert {"kind": "SUICIDAL_IDEATION", "item_key": "h3"} in body["risk_flags"]

    report = client.get(f"/api/v1/clinician/patients/{pid}/report", headers=c).json()
    assert report["last_assessment"]["total_score"] == 1
    assert report["history"][0]["total_score"] == 1

    audit = client.get("/api/v1/admin/audit", headers=a).json()["items"]
    assert "CLINICIAN_ASSESSMENT" in [e["action"] for e in audit]
    event = next(e for e in audit if e["action"] == "CLINICIAN_ASSESSMENT")
    assert event["ip"] == "testclient"

def test_clinician_assessment_patient_forbidden(client):
    h, pid = _with_patient(client, "passess@test.ru")
    resp = client.post(
        f"/api/v1/clinician/patients/{pid}/assessments",
        json={"questionnaire_code": "HDRS-17", "answers": HDRS_ALL_ZERO}, headers=h,
    )
    assert resp.status_code == 403
