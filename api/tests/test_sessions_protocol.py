"""FR-39: протокол приёма — версии, валидация §5.2, approve и шифрование в покое.

Контракт: docs/18 §3.4, §4.4, §5. Проверяются потолки разделов, обязательные
разделы для approve, блокировка после подтверждения, новая версия после подписи,
хэш канонического JSON, доступ (roster/IDOR/согласие) и аудит.
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import text

BASE = "/api/v1/clinician"

FULL = {
    "passport": {
        "visit_at": "2026-10-26T10:00:00+03:00",
        "channel": "in_person",
        "legal_basis": "ИДС № 12 от 2026-05-04",
    },
    "complaints": "тревога, бессонница",
    "anamnesis_morbi": "впервые в 2024",
    "mental_status": {
        "appearance_behavior": "опрятен",
        "consciousness_orientation": "ориентирован",
        "perception": "без обманов",
        "thinking_speech": "темп обычный",
        "emotions_volition": "тревожен",
        "criticism": "частичная",
        "suicidal_risk": "отрицает",
    },
    "somatic_status": "без особенностей",
    "diagnosis": {"syndrome": "тревожный", "icd10": "F41.1", "rationale": "по МКБ-10"},
    "treatment": [{"drug": "сертралин", "dose": "50 мг", "schedule": "утром", "duration": "6 мес"}],
    "recommendations": "КПТ, контроль через 2 недели",
}


def _ctx(client):
    """Врач с пациентом в roster и второй врач без доступа к этому пациенту."""
    from .conftest import admin_ctx, clinician_ctx, patient_ctx

    admin, _ = admin_ctx(client, "pr-admin@test.ru")
    hp, pid = patient_ctx(client, "pr-p1@test.ru")
    h_doc, cid = clinician_ctx(client, "pr-doc1@test.ru")
    h_doc2, _cid2 = clinician_ctx(client, "pr-doc2@test.ru")
    assert (
        client.post(
            "/api/v1/admin/roster",
            json={"patient_id": pid, "clinician_id": cid},
            headers=admin,
        ).status_code
        == 201
    )
    return {"patient": hp, "patient_id": pid, "doctor": h_doc, "doctor2": h_doc2}


def _session(client, ctx) -> dict:
    resp = client.post(
        f"{BASE}/sessions", json={"patient_id": ctx["patient_id"]}, headers=ctx["doctor"]
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _canonical(sections: dict) -> str:
    return json.dumps(sections, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_put_creates_draft_and_stores_ciphertext(client, db_session):
    ctx = _ctx(client)
    session = _session(client, ctx)
    resp = client.put(
        f"{BASE}/sessions/{session['id']}/protocol",
        json={"sections": {"complaints": "тревога, бессонница"}},
        headers=ctx["doctor"],
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["version"] == 1
    assert payload["status"] == "draft"
    assert payload["source"] == "manual"
    assert payload["content_sha256"] is None  # хэш появляется только при approve
    assert payload["sections"] == {"complaints": "тревога, бессонница"}

    fetched = client.get(f"{BASE}/sessions/{session['id']}/protocol", headers=ctx["doctor"])
    assert fetched.status_code == 200
    assert fetched.json()["sections"]["complaints"] == "тревога, бессонница"

    # В покое — bytea (pgcrypto), plaintext в сыром SELECT не читается (F1).
    raw = db_session.execute(
        text(
            "SELECT sections_cipher::text AS c FROM session_protocols "
            "WHERE session_id = CAST(:s AS uuid)"
        ).bindparams(s=session["id"])
    ).scalar_one()
    assert "тревога" not in raw
    assert "complaints" not in raw


def test_get_protocol_before_put_is_404(client):
    ctx = _ctx(client)
    session = _session(client, ctx)
    resp = client.get(f"{BASE}/sessions/{session['id']}/protocol", headers=ctx["doctor"])
    assert resp.status_code == 404
    assert resp.json()["code"] == "PROTOCOL_NOT_FOUND"


def test_section_limits_and_icd10(client):
    ctx = _ctx(client)
    session = _session(client, ctx)
    url = f"{BASE}/sessions/{session['id']}/protocol"

    too_long = client.put(
        url, json={"sections": {"complaints": "я" * 4001}}, headers=ctx["doctor"]
    )
    assert too_long.status_code == 422
    assert too_long.json()["code"] == "PROTOCOL_SECTION_TOO_LONG"

    mental = client.put(
        url,
        json={"sections": {"mental_status": {"suicidal_risk": "я" * 2001}}},
        headers=ctx["doctor"],
    )
    assert mental.status_code == 422
    assert mental.json()["code"] == "PROTOCOL_SECTION_TOO_LONG"

    many = [{"drug": f"препарат {i}"} for i in range(21)]
    treatment = client.put(url, json={"sections": {"treatment": many}}, headers=ctx["doctor"])
    assert treatment.status_code == 422
    assert treatment.json()["code"] == "PROTOCOL_SECTION_TOO_LONG"

    long_line = client.put(
        url,
        json={"sections": {"treatment": [{"drug": "я" * 501}]}},
        headers=ctx["doctor"],
    )
    assert long_line.status_code == 422
    assert long_line.json()["code"] == "PROTOCOL_SECTION_TOO_LONG"

    bad_code = client.put(
        url,
        json={"sections": {"diagnosis": {"icd10": "41.1"}}},
        headers=ctx["doctor"],
    )
    assert bad_code.status_code == 422
    assert bad_code.json()["code"] == "PROTOCOL_ICD10_INVALID"

    bad_body_code = client.put(
        url, json={"sections": {}, "diagnosis_code": "неверно"}, headers=ctx["doctor"]
    )
    assert bad_body_code.status_code == 422
    assert bad_body_code.json()["code"] == "PROTOCOL_ICD10_INVALID"


def test_approve_requires_mandatory_sections(client):
    ctx = _ctx(client)
    session = _session(client, ctx)
    url = f"{BASE}/sessions/{session['id']}/protocol"
    client.put(url, json={"sections": {"complaints": "тревога"}}, headers=ctx["doctor"])

    resp = client.post(f"{url}/approve", json={}, headers=ctx["doctor"])
    assert resp.status_code == 409
    assert resp.json()["code"] == "PROTOCOL_NOT_READY"
    detail = resp.json()["detail"]
    for name in ("passport.visit_at", "passport.legal_basis", "mental_status.suicidal_risk", "diagnosis.icd10"):
        assert name in detail
    missing = detail.split(": ", 1)[1].split(", ")
    assert "complaints" not in missing  # жалобы заполнены


def test_approve_hashes_canonical_json_and_locks(client, db_session):
    ctx = _ctx(client)
    session = _session(client, ctx)
    url = f"{BASE}/sessions/{session['id']}/protocol"
    client.put(url, json={"sections": FULL, "diagnosis_code": "F41.1"}, headers=ctx["doctor"])

    approved = client.post(f"{url}/approve", json={}, headers=ctx["doctor"])
    assert approved.status_code == 200, approved.text
    payload = approved.json()
    assert payload["status"] == "approved"
    assert payload["approved_by"]
    assert payload["approved_at"]
    assert payload["diagnosis_code"] == "F41.1"
    assert payload["content_sha256"] == hashlib.sha256(_canonical(FULL).encode()).hexdigest()

    # Повторный approve идемпотентен (документ не меняется).
    again = client.post(f"{url}/approve", json={}, headers=ctx["doctor"])
    assert again.status_code == 200
    assert again.json()["content_sha256"] == payload["content_sha256"]

    # Правка подтверждённого протокола запрещена (юридически значимая запись).
    locked = client.put(url, json={"sections": FULL}, headers=ctx["doctor"])
    assert locked.status_code == 409
    assert locked.json()["code"] == "PROTOCOL_LOCKED"

    rows = db_session.execute(
        text(
            "SELECT action FROM audit_log WHERE object_type = 'session_protocol' "
            "AND object_id = :oid"
        ).bindparams(oid=f"{session['id']}:v1")
    ).scalars().all()
    assert "SESSION_PROTOCOL_EDIT" in rows
    assert "SESSION_PROTOCOL_APPROVE" in rows


def test_edit_after_signed_creates_new_version(client, db_session):
    ctx = _ctx(client)
    session = _session(client, ctx)
    url = f"{BASE}/sessions/{session['id']}/protocol"
    client.put(url, json={"sections": FULL}, headers=ctx["doctor"])
    assert client.post(f"{url}/approve", json={}, headers=ctx["doctor"]).status_code == 200

    # Подпись ставит PR P4; здесь фиксируем состояние, в котором правка обязана
    # создать новую версию, а прежняя уйти в superseded (docs/18 §3.4).
    db_session.execute(
        text(
            "UPDATE session_protocols SET status = 'signed' WHERE session_id = CAST(:s AS uuid)"
        ).bindparams(s=session["id"])
    )
    db_session.commit()

    edited = client.put(
        url,
        json={"sections": {**FULL, "recommendations": "КПТ, контроль через неделю"}},
        headers=ctx["doctor"],
    )
    assert edited.status_code == 200, edited.text
    payload = edited.json()
    assert payload["version"] == 2
    assert payload["status"] == "draft"
    assert payload["content_sha256"] is None  # подпись не переносится на новые байты

    versions = db_session.execute(
        text(
            "SELECT version, status FROM session_protocols "
            "WHERE session_id = CAST(:s AS uuid) ORDER BY version"
        ).bindparams(s=session["id"])
    ).all()
    assert [(v, s) for v, s in versions] == [(1, "superseded"), (2, "draft")]

    # Новая версия подтверждается отдельно; активная версия в ответах — вторая.
    assert (
        client.post(f"{url}/approve", json={}, headers=ctx["doctor"]).status_code == 200
    )
    detail = client.get(f"{BASE}/sessions/{session['id']}", headers=ctx["doctor"]).json()
    assert detail["protocol"]["version"] == 2
    assert detail["protocol"]["status"] == "approved"


def test_protocol_access_gates(client):
    ctx = _ctx(client)
    session = _session(client, ctx)
    url = f"{BASE}/sessions/{session['id']}/protocol"
    client.put(url, json={"sections": FULL}, headers=ctx["doctor"])

    # Чужая сессия неотличима от несуществующей (IDOR, docs/18 §2).
    assert client.get(url, headers=ctx["doctor2"]).status_code == 404
    put = client.put(url, json={"sections": FULL}, headers=ctx["doctor2"])
    assert put.status_code == 404 and put.json()["code"] == "SESSION_NOT_FOUND"
    assert client.post(f"{url}/approve", json={}, headers=ctx["doctor2"]).status_code == 404

    # Отзыв согласия закрывает протокол (152-ФЗ ст.9).
    revoke = client.post("/api/v1/consent/revoke", json={}, headers=ctx["patient"])
    assert revoke.status_code in (200, 204), revoke.text
    after = client.get(url, headers=ctx["doctor"])
    assert after.status_code == 403
    assert after.json()["code"] == "CONSENT_REQUIRED"


def test_diagnosis_code_conflict_rejected(client):
    """`diagnosis_code` и `sections.diagnosis.icd10` — один диагноз, расхождение → 422."""
    ctx = _ctx(client)
    session = _session(client, ctx)
    url = f"{BASE}/sessions/{session['id']}/protocol"
    resp = client.put(
        url,
        json={"sections": FULL, "diagnosis_code": "F41.0"},
        headers=ctx["doctor"],
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "PROTOCOL_ICD10_CONFLICT"


def test_diagnosis_code_follows_sections_icd10(client, db_session):
    """Код МКБ берётся из разделов, если врач его не передал; approve закрепляет его."""
    ctx = _ctx(client)
    session = _session(client, ctx)
    url = f"{BASE}/sessions/{session['id']}/protocol"
    put = client.put(url, json={"sections": FULL}, headers=ctx["doctor"])
    assert put.status_code == 200, put.text
    assert put.json()["diagnosis_code"] == "F41.1"

    approve = client.post(f"{url}/approve", json={}, headers=ctx["doctor"])
    assert approve.status_code == 200, approve.text
    assert approve.json()["diagnosis_code"] == "F41.1"
    stored = db_session.execute(
        text(
            "SELECT diagnosis_code FROM session_protocols "
            "WHERE session_id = CAST(:s AS uuid)"
        ).bindparams(s=session["id"])
    ).scalar_one()
    assert stored == "F41.1"
