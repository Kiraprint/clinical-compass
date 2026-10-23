# E5: панель врача, roster, аудит доступа. Контракт: docs/04 §3.1; docs/06 §3.
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import event

from app.models import Assessment, AuditLog, ClinicianRoster, Escalation, Run, User
from app.services import clinician as clinician_service

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


def _seed_roster_case(db, clinician_id, count: int) -> list[str]:
    """Пациенты с перемешанными данными: GREEN/RED/YELLOW, без оценки,
    две оценки, открытые и закрытые эскалации. Возвращает id в порядке roster."""
    base = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    patient_ids: list[str] = []
    for i in range(count):
        patient = User(
            email=f"n1p{i}@test.ru",
            fio=f"Пациентов {i}",
            password_hash="x",
            role="PATIENT",
            phone="+79261234567" if i == 0 else None,
            birth_date=date(1990, 5, 15) if i == 0 else None,
        )
        db.add(patient)
        db.flush()
        patient_uuid = patient.id
        patient_ids.append(str(patient_uuid))
        db.add(ClinicianRoster(
            clinician_id=clinician_id,
            patient_id=patient_uuid,
            granted_at=base + timedelta(minutes=i),
        ))

        # 0..9 GREEN, 10..13 RED, 14 YELLOW, 15..17 без оценок,
        # 18 — две оценки (последняя RED), 19..23 RED.
        if i in range(15, 18):
            continue
        if i == 18:
            scores = ((3, "GREEN", base), (17, "RED", base + timedelta(days=1)))
        else:
            triage = "GREEN" if i < 10 else ("YELLOW" if i == 14 else "RED")
            scores = ((i, triage, base),)
        for total, triage, when in scores:
            run = Run(patient_id=patient_uuid, questionnaire_code="PHQ-9", mode="SCREENING",
                      finished_at=when)
            db.add(run)
            db.flush()
            db.add(Assessment(run_id=run.id, patient_id=patient_uuid, total_score=total,
                              severity="MODERATE", triage=triage, risk_flags=[], computed_at=when))

    # p10 — 2 открытые + 1 закрытая, p11 — 1 открытая, p12 — только закрытая.
    for idx, resolved in ((10, False), (10, False), (10, True), (11, False), (12, True)):
        db.add(Escalation(
            patient_id=uuid.UUID(patient_ids[idx]),
            kind="SEVERE",
            resolved_at=base + timedelta(days=2) if resolved else None,
        ))
    db.commit()
    return patient_ids


def test_list_patients_batches_queries(db_session):
    """N+1 → константа: <=4 запроса при любом размере roster + прежние поля/значения."""
    db = db_session
    clinician = User(email="n1c@test.ru", password_hash="x", role="CLINICIAN",
                     totp_secret="SECRET")
    db.add(clinician)
    db.flush()
    clinician_id = clinician.id
    patient_ids = _seed_roster_case(db, clinician_id, 24)

    statements: list[str] = []

    def _count(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    event.listen(db.bind, "before_cursor_execute", _count)
    try:
        cards = clinician_service.list_patients(db, clinician_id)
        full_count = len(statements)
        statements.clear()
        red_cards = clinician_service.list_patients(db, clinician_id, "RED")
        red_count = len(statements)
    finally:
        event.remove(db.bind, "before_cursor_execute", _count)

    assert full_count <= 4, statements
    assert red_count <= 4, statements
    assert full_count == red_count  # фильтр не добавляет запросов на пациента

    # Порядок показываем как порядок roster; никого не теряем.
    assert [c["patient_id"] for c in cards] == patient_ids
    assert len(cards) == len(patient_ids)
    assert set(cards[0]) == {
        "patient_id", "email", "fio", "last_triage", "last_score", "last_severity",
        "last_assessment_at", "open_escalations", "phone", "birth_date", "age",
    }

    by_id = {c["patient_id"]: c for c in cards}
    first = by_id[patient_ids[0]]
    assert first["last_triage"] == "GREEN"
    assert first["last_score"] == 0
    assert first["last_severity"] == "MODERATE"
    assert first["last_assessment_at"] == "2026-01-01T09:00:00+00:00"
    assert first["phone"] == "+7•••123-45-67"  # FR-22: только маска
    assert first["birth_date"] == "1990-05-15"

    no_assessment = by_id[patient_ids[15]]
    assert no_assessment["last_triage"] is None
    assert no_assessment["last_score"] is None
    assert no_assessment["last_severity"] is None
    assert no_assessment["last_assessment_at"] is None
    assert no_assessment["open_escalations"] == 0

    two_assessments = by_id[patient_ids[18]]
    assert two_assessments["last_triage"] == "RED"  # берём более новую
    assert two_assessments["last_score"] == 17

    assert by_id[patient_ids[10]]["open_escalations"] == 2  # закрытая не считается
    assert by_id[patient_ids[11]]["open_escalations"] == 1
    assert by_id[patient_ids[12]]["open_escalations"] == 0  # только закрытая
    assert by_id[patient_ids[13]]["open_escalations"] == 0

    assert [c["patient_id"] for c in red_cards] == [
        pid for pid in patient_ids if by_id[pid]["last_triage"] == "RED"
    ]
    assert all(c["last_triage"] == "RED" for c in red_cards)
    assert len(red_cards) == len(patient_ids) - 10 - 1 - 3  # не GREEN/YELLOW/без оценки
