"""FR-41 (docs/18 §4.5, §8): передача СЭМД в РЭМД — сквозной контур.

API и мок РЭМД общаются по настоящему HTTP (uvicorn в фоновом потоке), callback
мока идёт в HTTP-приёмник теста и оттуда — в `/api/v1/remd/callback`. Так
проверяются реальные 202+callback, идемпотентность, статусная машина и аудит на
живом Postgres, без подмены клиента РЭМД.
"""

from __future__ import annotations


import uuid

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import AuditLog

from . import protocol_signing as ps
from .remd_helpers import SNILS, CallbackSink, MockRemd

BASE = "/api/v1/clinician"
TOKEN = "test-callback-token"


@pytest.fixture()
def remd(monkeypatch):
    """Мок РЭМД + приёмник callback'ов + включённая интеграция в настройках."""
    with MockRemd() as mock, CallbackSink() as sink:
        monkeypatch.setenv("CC_REMD_MOCK_CALLBACK_TOKEN", TOKEN)
        monkeypatch.delenv("CC_REMD_MOCK_CALLBACK_URL", raising=False)
        monkeypatch.delenv("CC_REMD_MOCK_FORCE_STATUS", raising=False)
        monkeypatch.delenv("CC_REMD_MOCK_FAIL_FIRST", raising=False)
        monkeypatch.setattr(settings, "remd_enabled", True)
        monkeypatch.setattr(settings, "remd_base_url", mock.url)
        monkeypatch.setattr(settings, "remd_callback_url", sink.url)
        monkeypatch.setattr(settings, "remd_callback_token", TOKEN)
        monkeypatch.setattr(settings, "remd_allow_insecure", True)
        monkeypatch.setattr(settings, "remd_client_id", "compass-lpu")
        monkeypatch.setattr(settings, "remd_client_secret", "remd-secret")
        monkeypatch.setattr(settings, "remd_max_attempts", 3)
        monkeypatch.setattr(settings, "remd_patient_snils", SNILS)
        yield mock, sink


def _signed(client, prefix: str):
    context = ps.ctx(client, prefix)
    session = ps.approved(client, context)
    resp = client.post(
        f"{BASE}/sessions/{session['id']}/protocol/sign", headers=context["doctor"]
    )
    assert resp.status_code == 200, resp.text
    return session, context


def _submit(client, context, session):
    return client.post(
        f"{BASE}/sessions/{session['id']}/protocol/submit-emias",
        headers=context["doctor"],
    )


def _delivery(client, context, session):
    return client.get(f"{BASE}/sessions/{session['id']}/delivery", headers=context["doctor"])


def _callback(client, payload: dict, token: str | None = TOKEN):
    headers = {"X-Remd-Token": token} if token is not None else {}
    return client.post("/api/v1/remd/callback", json=payload, headers=headers)


def test_submit_requires_signed_protocol(client, remd):
    context = ps.ctx(client, "rd-unsigned")
    session = ps.approved(client, context)

    response = _submit(client, context, session)
    assert response.status_code == 409
    assert response.json()["code"] == "PROTOCOL_NOT_SIGNED"
    assert _delivery(client, context, session).status_code == 404


def test_disabled_integration_returns_503(client, monkeypatch):
    monkeypatch.setattr(settings, "remd_enabled", False)
    context = ps.ctx(client, "rd-disabled")
    session = ps.approved(client, context)

    response = _submit(client, context, session)
    assert response.status_code == 503
    assert response.json()["code"] == "EMIAS_DISABLED"


def test_happy_path_registers_document(client, remd, db_session):
    mock, sink = remd
    session, context = _signed(client, "rd-happy")

    response = _submit(client, context, session)
    assert response.status_code == 202, response.text
    delivery = response.json()
    assert delivery["status"] == "sending"
    assert delivery["attempts"] == 1
    assert delivery["request_id"]

    call = sink.wait(timeout=15)
    assert call is not None, "мок не доставил callback"
    payload = call["body"]
    assert payload["message_type"] == "registerDocumentResult"
    assert payload["status"] == "success"
    assert payload["response_to_request_id"] == delivery["request_id"]
    emdr_id = payload["data"]["emdr_id"]
    assert call["headers"]["X-Remd-Token"] == TOKEN

    accepted = _callback(client, payload)
    assert accepted.status_code == 200
    assert accepted.json() == {"lpu_uid": settings.remd_lpu_uid, "status": "success"}

    registered = _delivery(client, context, session).json()
    assert registered["status"] == "registered"
    assert registered["external_id"] == emdr_id
    assert registered["registered_at"] is not None

    detail = client.get(f"{BASE}/sessions/{session['id']}", headers=context["doctor"]).json()
    assert detail["delivery"]["status"] == "registered"
    assert detail["protocol"]["status"] == "submitted"

    actions = {
        row.action
        for row in db_session.scalars(
            select(AuditLog).where(AuditLog.object_id == session["id"])
        )
    }
    assert {"EMIAS_SUBMIT", "EMIAS_CALLBACK"} <= actions


def test_callback_is_idempotent(client, remd):
    _, sink = remd
    session, context = _signed(client, "rd-replay")
    assert _submit(client, context, session).status_code == 202
    payload = sink.wait(timeout=15)["body"]
    assert _callback(client, payload).status_code == 200
    first = _delivery(client, context, session).json()

    replay = _callback(client, payload)
    assert replay.status_code == 200 and replay.json()["status"] == "success"
    second = _delivery(client, context, session).json()
    assert second == first


def test_callback_requires_token(client, remd):
    _, sink = remd
    session, context = _signed(client, "rd-token")
    assert _submit(client, context, session).status_code == 202
    payload = sink.wait(timeout=15)["body"]

    assert _callback(client, payload, token=None).status_code == 401
    wrong = _callback(client, payload, token="nope")
    assert wrong.status_code == 401 and wrong.json()["code"] == "UNAUTHORIZED"

    unknown = dict(payload, response_to_request_id="00000000-0000-0000-0000-000000000000")
    assert _callback(client, unknown).status_code == 404

    other_message = dict(payload, message_type="getMetadataResult")
    rejected = _callback(client, other_message)
    assert rejected.status_code == 400
    assert rejected.json()["code"] == "VALIDATION_ERROR"


def test_repeated_submit_returns_existing_delivery(client, remd):
    mock, sink = remd
    session, context = _signed(client, "rd-repeat")
    first = _submit(client, context, session)
    assert first.status_code == 202
    payload = sink.wait(timeout=15)["body"]
    assert _callback(client, payload).status_code == 200
    calls = len(sink.calls)

    again = _submit(client, context, session)
    assert again.status_code == 200
    assert again.json()["request_id"] == first.json()["request_id"]
    assert again.json()["external_id"] == payload["data"]["emdr_id"]
    assert len(mock.registry) == 1
    assert len(sink.calls) == calls  # второго callback нет


def test_concurrent_submit_registers_once(client, remd):
    """Два одновременных submit: один request_id и одна запись реестра."""
    import asyncio

    from app import db as db_module
    from app.remd import service

    mock, _ = remd
    session, context = _signed(client, "rd-race")
    clinician_id = uuid.UUID(context["clinician_id"])
    session_id = uuid.UUID(session["id"])

    async def race():
        async with db_module.AsyncSessionLocal() as first, db_module.AsyncSessionLocal() as second:
            return await asyncio.gather(
                service.submit(first, clinician_id, session_id),
                service.submit(second, clinician_id, session_id),
            )

    results = asyncio.run(race())
    # Оба ответа 202: доставка ещё в `sending` (регистрация уже принята).
    assert [status for status, _ in results] == [202, 202]
    request_ids = {payload["request_id"] for _, payload in results}
    assert len(request_ids) == 1
    entries = [e for e in mock.registry.values() if e.get("local_document_uid")]
    assert len(entries) == 1
    assert mock.module._register_calls == 1


def test_tampered_cda_rejected(client, remd, db_session):
    session, context = _signed(client, "rd-tamper")
    detail = client.get(f"{BASE}/sessions/{session['id']}", headers=context["doctor"]).json()
    protocol_id = detail["protocol"]["id"]
    cda = ps.cda_bytes(db_session, protocol_id)
    ps.replace_cda(db_session, session["id"], protocol_id, cda + b"\n")

    response = _submit(client, context, session)
    assert response.status_code == 202, response.text
    delivery = response.json()
    assert delivery["status"] == "rejected"
    assert delivery["last_error_code"] == "INVALID_SIGNATURE"
    assert _delivery(client, context, session).json()["status"] == "rejected"


def test_transport_failure_marks_failed(client, remd, monkeypatch):
    mock, _ = remd
    monkeypatch.setenv("CC_REMD_MOCK_FORCE_STATUS", "500")
    monkeypatch.setattr(settings, "remd_max_attempts", 2)
    session, context = _signed(client, "rd-failed")

    response = _submit(client, context, session)
    assert response.status_code == 502
    assert response.json()["code"] == "EMIAS_UNAVAILABLE"

    delivery = _delivery(client, context, session).json()
    assert delivery["status"] == "failed"
    assert delivery["attempts"] == 1  # попытки отправки; HTTP-ретраи — в моке
    assert delivery["last_error_code"] == "EMIAS_UNAVAILABLE"
    assert mock.module._register_calls == 2  # клиент ретраил 5xx


def test_roster_gate(client, remd, db_session):
    """Подписанный протокол без roster: передача запрещена (403 NO_ROSTER)."""
    from app.models import ClinicianRoster

    session, context = _signed(client, "rd-roster")
    roster = db_session.get(ClinicianRoster, (uuid.UUID(context["clinician_id"]),
                                             uuid.UUID(context["patient_id"])))
    db_session.delete(roster)
    db_session.commit()

    response = _submit(client, context, session)
    assert response.status_code == 403
    assert response.json()["code"] == "NO_ROSTER"


def test_consent_gate(client, remd):
    session, context = _signed(client, "rd-consent")
    assert client.post("/api/v1/consent/revoke", headers=context["patient"]).status_code == 204

    response = _submit(client, context, session)
    assert response.status_code == 403
    assert response.json()["code"] == "CONSENT_REQUIRED"
