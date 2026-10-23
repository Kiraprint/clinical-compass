# E5: панель врача, roster, аудит доступа. Контракт: docs/04 §3.1; docs/06 §3.
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.models import Assessment, AuditLog, Escalation

from .conftest import admin_ctx, auth_header, clinician_ctx, finish_screening, patient_ctx

PHQ_ALL = {f"q{i}": 0 for i in range(1, 10)}

MOSCOW = ZoneInfo("Europe/Moscow")

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


def test_report_exposes_scale_anchors(client):
    """Карточка врача отдаёт подписи вариантов — HDRS-17 заполняется по якорям, не числами."""
    _, pid = _with_patient(client, "anch@test.ru")
    c, cid = clinician_ctx(client, "anchdoc@test.ru")
    a, _ = admin_ctx(client, "anchadm@test.ru")
    _grant(client, a, pid, cid)
    body = client.get(f"/api/v1/clinician/patients/{pid}/report", headers=c).json()
    scales = {s["code"]: s for s in body["available_scales"]}
    assert "HDRS-17" in scales
    for scale in scales.values():
        for item in scale["items"]:
            assert len(item["anchors"]) == item["max_value"] + 1, (scale["code"], item["key"])
    hdrs = {i["key"]: i for i in scales["HDRS-17"]["items"]}
    assert len(hdrs["h1"]["anchors"]) == 5
    assert len(hdrs["h4"]["anchors"]) == 3


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
    """FR-27: врач видит weighted-процент от ожидаемых приёмов по режиму назначения."""
    h, pid = _with_patient(client, "ad@test.ru")
    c, cid = clinician_ctx(client, "adc@test.ru")
    a, _ = admin_ctx(client, "ada@test.ru")
    _grant(client, a, pid, cid)
    med = client.post(
        f"/api/v1/clinician/patients/{pid}/medications",
        json={"title": "Авиандр", "started_on": datetime.now(MOSCOW).date().isoformat(), "times_per_day": 2},
        headers=c,
    ).json()
    # Назначение с сегодняшнего дня: ожидается 2 приёма. taken + partial = 1.5 → 75%.
    for st in ("taken", "partial"):
        r = client.post(
            "/api/v1/me/adherence", json={"medication_id": med["id"], "status": st}, headers=h,
        )
        assert r.status_code == 201, r.text
    # Историческая отметка без связи с назначением — вне знаменателя.
    r = client.post("/api/v1/me/adherence", json={"medication": "Авиандр", "status": "missed"}, headers=h)
    assert r.status_code == 201, r.text

    summ = client.get(f"/api/v1/clinician/patients/{pid}/adherence", headers=c).json()
    assert summ["expected"] == 2 and summ["score"] == 1.5
    assert summ["pct"] == 75
    assert summ["total"] == 3 and summ["taken"] == 1 and summ["partial"] == 1 and summ["missed"] == 1
    assert summ["excluded_count"] == 1
    assert summ["medications"][0]["title"] == "Авиандр"

    # Агрегат есть и в отчёте
    report = client.get(f"/api/v1/clinician/patients/{pid}/report", headers=c).json()
    assert report["adherence"]["pct"] == 75
    assert report["adherence"]["expected"] == 2
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

# --- FR-30: порядок карточек врача (красные всегда сверху) ---


def test_patients_sorted_red_first_without_assessment_last(client):
    _, pid_green = _with_patient(client, "sort-g@test.ru", PHQ_ALL)  # 0 → GREEN
    _, pid_yellow = _with_patient(client, "sort-y@test.ru", {**PHQ_ALL, "q1": 3, "q2": 3})  # 6 → YELLOW
    _, pid_red = _with_patient(client, "sort-r@test.ru", {**PHQ_ALL, "q9": 2})  # флаг → RED
    _, pid_none = patient_ctx(client, "sort-n@test.ru")  # в ростере, без оценки
    c, cid = clinician_ctx(client, "sortc@test.ru")
    a, _ = admin_ctx(client, "sorta@test.ru")
    for pid in (pid_green, pid_yellow, pid_red, pid_none):
        _grant(client, a, pid, cid)

    cards = client.get("/api/v1/clinician/patients", headers=c).json()["items"]
    assert [card["patient_id"] for card in cards] == [pid_red, pid_yellow, pid_green, pid_none]
    assert [card["last_triage"] for card in cards] == ["RED", "YELLOW", "GREEN", None]

    # ?triage= по-прежнему фильтрует и не пересортировывает.
    red_only = client.get("/api/v1/clinician/patients?triage=RED", headers=c).json()["items"]
    assert [card["patient_id"] for card in red_only] == [pid_red]


def test_patients_sorted_by_escalations_then_freshness(client, db_session):
    _, pid_quiet = _with_patient(client, "ord-a@test.ru", PHQ_ALL)
    _, pid_esc = _with_patient(client, "ord-b@test.ru", PHQ_ALL)
    c, cid = clinician_ctx(client, "ordc@test.ru")
    a, _ = admin_ctx(client, "orda@test.ru")
    _grant(client, a, pid_quiet, cid)
    _grant(client, a, pid_esc, cid)

    # Одинаковый триаж GREEN: впереди тот, у кого больше открытых эскалаций.
    db_session.add(Escalation(patient_id=uuid.UUID(pid_esc), kind="CHECKIN_RISK"))
    db_session.commit()
    cards = client.get("/api/v1/clinician/patients", headers=c).json()["items"]
    assert [card["patient_id"] for card in cards] == [pid_esc, pid_quiet]

    # Эскалации равны (0): впереди свежая оценка; старую состарили явно.
    db_session.query(Escalation).filter(Escalation.patient_id == uuid.UUID(pid_esc)).delete()
    db_session.query(Assessment).filter(Assessment.patient_id == uuid.UUID(pid_quiet)).update(
        {"computed_at": datetime(2020, 1, 1, tzinfo=timezone.utc)}
    )
    db_session.commit()
    cards = client.get("/api/v1/clinician/patients", headers=c).json()["items"]
    assert [card["patient_id"] for card in cards] == [pid_esc, pid_quiet]


# --- FR-29: список переписок врача ---


def test_clinician_chats_only_roster_sorted_with_preview(client):
    _, pid_first = patient_ctx(client, "chatf@test.ru")
    _, pid_last = patient_ctx(client, "chatl@test.ru")
    _, pid_foreign = patient_ctx(client, "chatx@test.ru")  # не в ростере
    c, cid = clinician_ctx(client, "chatsc@test.ru")
    a, _ = admin_ctx(client, "chatsa@test.ru")
    _grant(client, a, pid_first, cid)
    _grant(client, a, pid_last, cid)

    long_text = "тревога " * 40  # > 120 символов
    assert client.post(
        f"/api/v1/clinician/patients/{pid_first}/chat", json={"content": "короткое"}, headers=c
    ).status_code == 201
    assert client.post(
        f"/api/v1/clinician/patients/{pid_last}/chat", json={"content": long_text}, headers=c
    ).status_code == 201

    items = client.get("/api/v1/clinician/chats", headers=c).json()["items"]
    assert [item["patient_id"] for item in items] == [pid_last, pid_first]
    assert all(item["patient_id"] != pid_foreign for item in items)

    head = items[0]
    assert head["last_sender"] == "CLINICIAN"
    assert head["message_count"] == 1
    assert len(head["preview"]) <= 120
    assert head["preview"].endswith("…")
    assert head["preview"].startswith("тревога")  # дешифровано сервером
    assert items[1]["preview"] == "короткое"


def test_clinician_chats_scoped_to_clinician_and_role(client):
    _, pid = patient_ctx(client, "chatscope@test.ru")
    c1, cid1 = clinician_ctx(client, "chatd1@test.ru")
    c2, _ = clinician_ctx(client, "chatd2@test.ru")
    a, _ = admin_ctx(client, "chatda@test.ru")
    _grant(client, a, pid, cid1)
    client.post(f"/api/v1/clinician/patients/{pid}/chat", json={"content": "привет"}, headers=c1)

    assert len(client.get("/api/v1/clinician/chats", headers=c1).json()["items"]) == 1
    assert client.get("/api/v1/clinician/chats", headers=c2).json()["items"] == []

    h, _ = patient_ctx(client, "chatrole@test.ru")
    resp = client.get("/api/v1/clinician/chats", headers=h)
    assert resp.status_code == 403
    assert resp.json()["code"] == "ROLE_MISMATCH"
