"""FR-38: календарь приёмов и сессии врача — контракт docs/18 §3.1–3.3, §4.1–4.2.

Проверяется поведение, а не реализация: RBAC/IDOR (чужой врач и пациент вне
roster), обязательное активное согласие, конфликт слота, идемпотентность старта
и завершения (ровно один приём), offline-приём без записи, аудит записи.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from .conftest import admin_ctx, auth_header, clinician_ctx, login, patient_ctx, register

BASE = "/api/v1/clinician"


def _roster(client, admin: dict, clinician_id: str, patient_id: str) -> None:
    resp = client.post(
        "/api/v1/admin/roster",
        json={"patient_id": patient_id, "clinician_id": clinician_id},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text


def _setup(client):
    """Админ, врач с пациентом в roster, второй врач без доступа, пациент без согласия."""
    admin, _ = admin_ctx(client, "s-admin@test.ru")
    hp, p1 = patient_ctx(client, "s-p1@test.ru")
    h_doc, c1 = clinician_ctx(client, "s-doc1@test.ru")
    h_doc2, c2 = clinician_ctx(client, "s-doc2@test.ru")
    _roster(client, admin, c1, p1)
    # Пациент без согласия 152-ФЗ (регистрация без POST /consent) + roster.
    reg = register(client, "s-noconsent@test.ru")
    h_nc = auth_header(login(client, "s-noconsent@test.ru")["access_token"])
    _roster(client, admin, c1, reg["id"])
    return {
        "admin": admin,
        "patient": hp,
        "patient_id": p1,
        "doctor": h_doc,
        "doctor_id": c1,
        "doctor2": h_doc2,
        "doctor2_id": c2,
        "no_consent": h_nc,
        "no_consent_id": reg["id"],
    }


def _slot(hours: int = 0, minutes: int = 0) -> str:
    """Время слота: ближайший час + смещение (детерминированно внутри теста)."""
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return (base + timedelta(hours=hours, minutes=minutes)).isoformat()


def _appointment(client, ctx, **body) -> dict:
    payload = {"patient_id": ctx["patient_id"], "starts_at": _slot(hours=30)}
    payload.update(body)
    resp = client.post(f"{BASE}/appointments", json=payload, headers=ctx["doctor"])
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_calendar_requires_clinician_role(client):
    ctx = _setup(client)
    resp = client.get(
        f"{BASE}/appointments",
        params={"from": _slot(), "to": _slot(hours=24)},
        headers=ctx["patient"],
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "ROLE_MISMATCH"


def test_appointment_create_and_shape(client):
    ctx = _setup(client)
    appt = _appointment(client, ctx, duration_min=45, channel="online", location="каб. 12")
    assert appt["patient_id"] == ctx["patient_id"]
    assert "patient_fio" in appt  # ФИО может быть не заполнено у пациента
    assert appt["duration_min"] == 45 and appt["channel"] == "online"
    assert appt["status"] == "planned"
    assert appt["location"] == "каб. 12"
    assert appt["has_session"] is False

    listed = client.get(
        f"{BASE}/appointments",
        params={"from": _slot(), "to": _slot(hours=48)},
        headers=ctx["doctor"],
    )
    assert listed.status_code == 200, listed.text
    assert [i["id"] for i in listed.json()["items"]] == [appt["id"]]


def test_calendar_window_capped(client):
    ctx = _setup(client)
    resp = client.get(
        f"{BASE}/appointments",
        params={"from": _slot(), "to": _slot(hours=24 * 63)},
        headers=ctx["doctor"],
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"



def test_calendar_pagination(client):
    """§4: limit/offset с потолком 200 — не «всё за окно одной пачкой»."""
    ctx = _setup(client)
    first = _appointment(client, ctx, starts_at=_slot(hours=30))
    second = _appointment(client, ctx, starts_at=_slot(hours=40))
    params = {"from": _slot(), "to": _slot(hours=24 * 7)}

    page = client.get(f"{BASE}/appointments", params={**params, "limit": 1}, headers=ctx["doctor"])
    assert page.status_code == 200, page.text
    assert [item["id"] for item in page.json()["items"]] == [first["id"]]

    tail = client.get(
        f"{BASE}/appointments", params={**params, "limit": 1, "offset": 1}, headers=ctx["doctor"]
    )
    assert [item["id"] for item in tail.json()["items"]] == [second["id"]]

    too_big = client.get(
        f"{BASE}/appointments", params={**params, "limit": 201}, headers=ctx["doctor"]
    )
    assert too_big.status_code == 422


def test_roster_gate_and_consent(client):
    ctx = _setup(client)
    # Пациент второго врача вне roster → 403 NO_ROSTER (существующий код, docs/03 §5).
    resp = client.get(
        f"{BASE}/appointments",
        params={"from": _slot(), "to": _slot(hours=24), "patient_id": ctx["patient_id"]},
        headers=ctx["doctor2"],
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "NO_ROSTER"

    # Активного согласия нет → 403 CONSENT_REQUIRED (152-ФЗ ст.9).
    resp = client.post(
        f"{BASE}/appointments",
        json={"patient_id": ctx["no_consent_id"], "starts_at": _slot(hours=30)},
        headers=ctx["doctor"],
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "CONSENT_REQUIRED"


def test_slot_conflict_and_cancel_frees_slot(client):
    ctx = _setup(client)
    first = _appointment(client, ctx, starts_at=_slot(hours=30), duration_min=50)

    clash = client.post(
        f"{BASE}/appointments",
        json={"patient_id": ctx["patient_id"], "starts_at": _slot(hours=30, minutes=30)},
        headers=ctx["doctor"],
    )
    assert clash.status_code == 409
    assert clash.json()["code"] == "SLOT_CONFLICT"

    # Соседний слот после конца первого приёма — свободен.
    _appointment(client, ctx, starts_at=_slot(hours=32))

    # Отмена освобождает интервал: пересечение снова разрешено.
    patched = client.patch(
        f"{BASE}/appointments/{first['id']}",
        json={"status": "cancelled"},
        headers=ctx["doctor"],
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["status"] == "cancelled"
    _appointment(client, ctx, starts_at=_slot(hours=30, minutes=30))


def test_terminal_appointment_not_rescheduled_and_not_started(client):
    ctx = _setup(client)
    appt = _appointment(client, ctx, starts_at=_slot(hours=30))
    assert (
        client.patch(
            f"{BASE}/appointments/{appt['id']}",
            json={"status": "no_show"},
            headers=ctx["doctor"],
        ).status_code
        == 200
    )

    moved = client.patch(
        f"{BASE}/appointments/{appt['id']}",
        json={"starts_at": _slot(hours=40)},
        headers=ctx["doctor"],
    )
    assert moved.status_code == 409
    assert moved.json()["code"] == "APPOINTMENT_NOT_ACTIVE"

    start = client.post(f"{BASE}/appointments/{appt['id']}/start", json={}, headers=ctx["doctor"])
    assert start.status_code == 409
    assert start.json()["code"] == "APPOINTMENT_NOT_ACTIVE"


def test_start_appointment_is_idempotent(client):
    ctx = _setup(client)
    appt = _appointment(client, ctx, starts_at=_slot(hours=30), channel="phone")

    first = client.post(f"{BASE}/appointments/{appt['id']}/start", json={}, headers=ctx["doctor"])
    assert first.status_code == 201, first.text
    session = first.json()
    assert session["session_id"] == session["id"]  # контракт §4.1: {session_id}
    assert session["appointment_id"] == appt["id"]
    assert session["channel"] == "phone"
    assert session["status"] == "active"
    assert session["patient_id"] == ctx["patient_id"]
    assert session["ai_status"] == "off" and session["audio_consent"] is False
    assert session["protocol"] is None

    again = client.post(f"{BASE}/appointments/{appt['id']}/start", json={}, headers=ctx["doctor"])
    assert again.status_code == 200
    assert again.json()["id"] == session["id"]

    listed = client.get(
        f"{BASE}/appointments",
        params={"from": _slot(), "to": _slot(hours=48)},
        headers=ctx["doctor"],
    )
    item = next(i for i in listed.json()["items"] if i["id"] == appt["id"])
    assert item["has_session"] is True


def test_offline_session_without_appointment(client):
    ctx = _setup(client)
    resp = client.post(
        f"{BASE}/sessions", json={"patient_id": ctx["patient_id"]}, headers=ctx["doctor"]
    )
    assert resp.status_code == 201, resp.text
    session = resp.json()
    assert session["appointment_id"] is None
    assert session["channel"] == "in_person"

    second = client.post(
        f"{BASE}/sessions", json={"patient_id": ctx["patient_id"]}, headers=ctx["doctor"]
    )
    assert second.status_code == 201
    assert second.json()["id"] != session["id"]  # offline-сессии не ограничены


def test_session_idor_returns_404(client):
    ctx = _setup(client)
    session = client.post(
        f"{BASE}/sessions", json={"patient_id": ctx["patient_id"]}, headers=ctx["doctor"]
    ).json()

    assert client.get(f"{BASE}/sessions/{session['id']}", headers=ctx["doctor2"]).status_code == 404
    finish = client.post(
        f"{BASE}/sessions/{session['id']}/finish", json={}, headers=ctx["doctor2"]
    )
    assert finish.status_code == 404
    assert finish.json()["code"] == "SESSION_NOT_FOUND"
    patch = client.patch(
        f"{BASE}/sessions/{session['id']}", json={"audio_consent": True}, headers=ctx["doctor2"]
    )
    assert patch.status_code == 404


def test_finish_creates_exactly_one_visit(client, db_session):
    ctx = _setup(client)
    session = client.post(
        f"{BASE}/sessions",
        json={"patient_id": ctx["patient_id"], "channel": "online"},
        headers=ctx["doctor"],
    ).json()
    sections = {
        "passport": {"visit_at": _slot(hours=-1), "legal_basis": "ИДС № 12"},
        "complaints": "тревога, бессонница",
        "mental_status": {"suicidal_risk": "отрицает"},
        "diagnosis": {"icd10": "F41.1"},
        "recommendations": "КПТ, контроль через 2 недели",
    }
    assert (
        client.put(
            f"{BASE}/sessions/{session['id']}/protocol",
            json={"sections": sections},
            headers=ctx["doctor"],
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"{BASE}/sessions/{session['id']}/protocol/approve", json={}, headers=ctx["doctor"]
        ).status_code
        == 200
    )

    done = client.post(
        f"{BASE}/sessions/{session['id']}/finish", json={"duration_min": 50}, headers=ctx["doctor"]
    )
    assert done.status_code == 200, done.text
    payload = done.json()
    assert payload["status"] == "finished" and payload["ended_at"]
    assert payload["protocol"]["status"] == "approved"
    visit_id = payload["visit_id"]
    assert visit_id

    # Повторный finish идемпотентен: тот же приём, второй не создаётся.
    again = client.post(f"{BASE}/sessions/{session['id']}/finish", json={}, headers=ctx["doctor"])
    assert again.status_code == 200
    assert again.json()["visit_id"] == visit_id

    count = db_session.execute(
        text("SELECT count(*) FROM visits WHERE session_id = CAST(:s AS uuid)").bindparams(
            s=session["id"]
        )
    ).scalar_one()
    assert count == 1

    visits = client.get(f"{BASE}/patients/{ctx['patient_id']}/visits", headers=ctx["doctor"])
    assert visits.status_code == 200, visits.text
    item = next(v for v in visits.json()["items"] if v["id"] == visit_id)
    assert item["visit_type"] == "online" and item["duration_min"] == 50
    # Сводка подтверждённого протокола попала в приём, текст расшифрован для врача.
    assert "Жалобы: тревога, бессонница" in item["notes"]
    assert "F41.1" in item["notes"]


def test_finish_without_protocol_leaves_empty_summary(client):
    ctx = _setup(client)
    session = client.post(
        f"{BASE}/sessions", json={"patient_id": ctx["patient_id"]}, headers=ctx["doctor"]
    ).json()
    done = client.post(f"{BASE}/sessions/{session['id']}/finish", json={}, headers=ctx["doctor"])
    assert done.status_code == 200
    visits = client.get(f"{BASE}/patients/{ctx['patient_id']}/visits", headers=ctx["doctor"])
    item = next(v for v in visits.json()["items"] if v["id"] == done.json()["visit_id"])
    assert item["notes"] == ""


def test_patch_session_audio_consent(client):
    ctx = _setup(client)
    session = client.post(
        f"{BASE}/sessions", json={"patient_id": ctx["patient_id"]}, headers=ctx["doctor"]
    ).json()
    resp = client.patch(
        f"{BASE}/sessions/{session['id']}", json={"audio_consent": True}, headers=ctx["doctor"]
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["audio_consent"] is True

    listed = client.get(
        f"{BASE}/sessions",
        params={"from": _slot(), "to": _slot(hours=24), "status": "active"},
        headers=ctx["doctor"],
    )
    assert listed.status_code == 200
    assert [i["id"] for i in listed.json()["items"]] == [session["id"]]


def test_session_detail_contains_protocol_and_artifacts(client):
    ctx = _setup(client)
    session = client.post(
        f"{BASE}/sessions", json={"patient_id": ctx["patient_id"]}, headers=ctx["doctor"]
    ).json()
    client.put(
        f"{BASE}/sessions/{session['id']}/protocol",
        json={"sections": {"complaints": "головная боль"}},
        headers=ctx["doctor"],
    )
    detail = client.get(f"{BASE}/sessions/{session['id']}", headers=ctx["doctor"])
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["protocol"]["version"] == 1
    assert payload["protocol"]["status"] == "draft"
    assert payload["artifacts"] == []
    # Ключи будущих PR присутствуют в контракте ответа (§4.2).
    assert payload["signature"] is None and payload["delivery"] is None


def test_audit_rows_written_for_write_paths(client, db_session):
    ctx = _setup(client)
    appt = _appointment(client, ctx, starts_at=_slot(hours=30))
    session = client.post(
        f"{BASE}/appointments/{appt['id']}/start", json={}, headers=ctx["doctor"]
    ).json()
    client.patch(
        f"{BASE}/appointments/{appt['id']}", json={"location": "каб. 3"}, headers=ctx["doctor"]
    )
    client.post(f"{BASE}/sessions/{session['id']}/finish", json={}, headers=ctx["doctor"])
    client.patch(
        f"{BASE}/sessions/{session['id']}",
        json={"audio_consent": True},
        headers=ctx["doctor"],
    )

    actions = {
        row.action
        for row in db_session.execute(
            text(
                "SELECT action FROM audit_log WHERE actor_id = CAST(:a AS uuid) "
                "AND object_type IN ('appointment', 'clinical_session')"
            ).bindparams(a=ctx["doctor_id"])
        )
    }
    assert {"APPOINTMENT_CREATE", "APPOINTMENT_UPDATE", "SESSION_START", "SESSION_FINISH"} <= actions
    # В журнале нет текста протокола/заметок — только ids (docs/18 §2).
    blob = db_session.execute(
        text("SELECT coalesce(string_agg(action, ' '), '') FROM audit_log")
    ).scalar_one()
    assert "тревога" not in blob


def test_consent_update_is_audited(client, db_session):
    """PATCH `audio_consent` — отдельное основание обработки, значит и отдельный аудит."""
    ctx = _setup(client)
    session = client.post(
        f"{BASE}/sessions", json={"patient_id": ctx["patient_id"]}, headers=ctx["doctor"]
    ).json()
    resp = client.patch(
        f"{BASE}/sessions/{session['id']}",
        json={"audio_consent": True},
        headers=ctx["doctor"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["audio_consent"] is True

    row = db_session.execute(
        text(
            "SELECT object_type, object_id FROM audit_log "
            "WHERE action = 'SESSION_CONSENT_UPDATE' AND actor_id = CAST(:a AS uuid)"
        ).bindparams(a=ctx["doctor_id"])
    ).one()
    assert row[0] == "clinical_session"
    assert str(row[1]) == session["id"]


def test_naive_datetime_rejected(client):
    """ISO-8601 без пояса неоднозначен — 422 вместо TypeError/сдвига времени."""
    ctx = _setup(client)
    naive = client.post(
        f"{BASE}/appointments",
        json={"patient_id": ctx["patient_id"], "starts_at": "2026-11-02T10:00:00"},
        headers=ctx["doctor"],
    )
    assert naive.status_code == 422, naive.text
    assert naive.json()["code"] == "VALIDATION_ERROR"

    window = client.get(
        f"{BASE}/appointments",
        params={"from": "2026-11-01T00:00:00", "to": "2026-11-02T00:00:00+00:00"},
        headers=ctx["doctor"],
    )
    assert window.status_code == 422, window.text


def test_sessions_window_validated(client):
    """То же окно, что и в календаре: без пояса и с `to<from` — 422, а не пустой список."""
    ctx = _setup(client)
    naive = client.get(
        f"{BASE}/sessions",
        params={"from": "2026-11-01T00:00:00", "to": "2026-11-02T00:00:00+00:00"},
        headers=ctx["doctor"],
    )
    assert naive.status_code == 422, naive.text
    inverted = client.get(
        f"{BASE}/sessions",
        params={"from": _slot(hours=24), "to": _slot()},
        headers=ctx["doctor"],
    )
    assert inverted.status_code == 422, inverted.text


def test_lists_hide_non_consented_patients(client):
    """Отзыв согласия убирает пациента из списков врача (152-ФЗ, docs/18 §2)."""
    ctx = _setup(client)
    appt = _appointment(client, ctx, starts_at=_slot(hours=30))
    session = client.post(
        f"{BASE}/sessions", json={"patient_id": ctx["patient_id"]}, headers=ctx["doctor"]
    ).json()
    params = {"from": _slot(), "to": _slot(hours=24 * 7)}

    visible = client.get(f"{BASE}/appointments", params=params, headers=ctx["doctor"])
    assert [item["id"] for item in visible.json()["items"]] == [appt["id"]]
    assert client.get(f"{BASE}/sessions", params=params, headers=ctx["doctor"]).json()["items"]

    revoke = client.post("/api/v1/consent/revoke", json={}, headers=ctx["patient"])
    assert revoke.status_code in (200, 204), revoke.text

    assert client.get(f"{BASE}/appointments", params=params, headers=ctx["doctor"]).json() == {
        "items": []
    }
    assert client.get(f"{BASE}/sessions", params=params, headers=ctx["doctor"]).json() == {
        "items": []
    }
    # Прямой доступ к самой сессии по-прежнему закрыт гейтом согласия.
    assert client.get(f"{BASE}/sessions/{session['id']}", headers=ctx["doctor"]).status_code == 403


def test_patch_audio_consent_null_rejected(client):
    """`null` не должен читаться как «согласие отозвано» — только явный false."""
    ctx = _setup(client)
    session = client.post(
        f"{BASE}/sessions",
        json={"patient_id": ctx["patient_id"], "audio_consent": True},
        headers=ctx["doctor"],
    ).json()
    resp = client.patch(
        f"{BASE}/sessions/{session['id']}",
        json={"audio_consent": None},
        headers=ctx["doctor"],
    )
    assert resp.status_code == 422, resp.text
    after = client.get(f"{BASE}/sessions/{session['id']}", headers=ctx["doctor"]).json()
    assert after["audio_consent"] is True
