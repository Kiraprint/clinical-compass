"""FR-41 (docs/18 §4.5, §8): передача СЭМД в РЭМД — сквозной контур.

API и мок РЭМД общаются по настоящему HTTP (uvicorn в фоновом потоке), callback
мока идёт в HTTP-приёмник теста и оттуда — в `/api/v1/remd/callback`. Так
проверяются реальные 202+callback, идемпотентность, статусная машина и аудит на
живом Postgres, без подмены клиента РЭМД.
"""

from __future__ import annotations


import uuid

import pytest
from sqlalchemy import select, text

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

def test_callback_body_cap(client, remd, monkeypatch):
    """Потолок тела считается до чтения: Content-Length и chunked одинаково 413."""
    monkeypatch.setattr(settings, "remd_callback_max_kb", 1)
    url = "/api/v1/remd/callback"
    headers = {"X-Remd-Token": TOKEN, "Content-Type": "application/json"}
    big = b"{" + b" " * (1024 * 1024) + b"}"

    declared = client.post(url, content=big, headers=headers)
    assert declared.status_code == 413
    assert declared.json()["code"] == "PAYLOAD_TOO_LARGE"

    # Без Content-Length (chunked): отсекаем по ходу чтения потока.
    chunked = client.post(url, content=iter([big[i : i + 8192] for i in range(0, len(big), 8192)]), headers=headers)
    assert chunked.status_code == 413
    assert chunked.json()["code"] == "PAYLOAD_TOO_LARGE"


def test_repeated_submit_returns_existing_delivery(client, remd):
    mock, sink = remd
    session, context = _signed(client, "rd-repeat")
    first = _submit(client, context, session)
    assert first.status_code == 202
    payload = sink.wait(timeout=15)["body"]
    assert _callback(client, payload).status_code == 200
    calls = len(sink.calls)

    again = _submit(client, context, session)
    assert again.status_code == 200, again.json()
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
    # Ровно один request_id, и он есть в каждом ответе: конкурент видит уже
    # сохранённый id, а не `null` из гонки.
    request_ids = [payload["request_id"] for _, payload in results]
    assert all(request_ids), request_ids
    assert len(set(request_ids)) == 1
    session_row = client.get(f"{BASE}/sessions/{session['id']}", headers=context["doctor"]).json()
    assert session_row["delivery"]["request_id"] == request_ids[0]
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


def test_stale_sending_becomes_failed_then_retried(client, remd, monkeypatch):
    """Потерянный callback: `sending` протухает в `failed`, а клик после — повтор (P2)."""
    mock, sink = remd
    monkeypatch.setenv("CC_REMD_MOCK_CALLBACK_ATTEMPTS", "1")
    sink.status = 500
    session, context = _signed(client, "rd-stale")
    assert _submit(client, context, session).status_code == 202
    sink.wait_calls(1, timeout=10)
    first = _delivery(client, context, session).json()
    assert first["status"] == "sending" and first["external_id"] is None

    monkeypatch.setattr(settings, "remd_sending_stale_sec", 0)
    expired = _submit(client, context, session)
    assert expired.status_code == 202
    assert expired.json()["status"] == "failed"
    assert expired.json()["last_error_code"] == "CALLBACK_TIMEOUT"
    assert mock.module._register_calls == 1  # протухание само ничего не отправляет

    again = _submit(client, context, session)
    assert again.status_code == 202
    assert again.json()["status"] == "sending"
    assert again.json()["attempts"] == 2
    assert mock.module._register_calls == 2
    assert again.json()["request_id"] == first["request_id"]  # тот же документ, тот же UID


def test_stale_threshold_default_keeps_fresh_sending(client, remd, monkeypatch):
    """Без протухания доставка не переводится в `failed` (дефолтные 900 с)."""
    mock, sink = remd
    monkeypatch.setenv("CC_REMD_MOCK_CALLBACK_ATTEMPTS", "1")
    sink.status = 500
    session, context = _signed(client, "rd-fresh")
    assert _submit(client, context, session).status_code == 202
    sink.wait_calls(1, timeout=10)
    repeat = _submit(client, context, session)
    assert repeat.status_code == 202
    assert repeat.json()["status"] == "sending"
    assert mock.module._register_calls == 1


def test_retry_after_failed_uses_full_envelope(client, remd, monkeypatch):
    """Повтор после `failed` отправляет полный конверт, а не пустой (P1)."""
    mock, _ = remd
    from app.remd.client import RemdClient, RemdRejected

    calls: list[dict] = []
    original = RemdClient.register_document

    def recording(self, envelope):
        calls.append(dict(envelope))
        return original(self, envelope)

    monkeypatch.setattr(RemdClient, "register_document", recording)
    session, context = _signed(client, "rd-envelope")
    monkeypatch.setattr(
        RemdClient,
        "register_document",
        lambda self, envelope: (_ for _ in ()).throw(
            RemdRejected("INTERNAL_ERROR", "сбой", 500)
        ),
    )
    assert _submit(client, context, session).status_code == 502
    assert _delivery(client, context, session).json()["status"] == "failed"

    monkeypatch.setattr(RemdClient, "register_document", recording)
    retried = _submit(client, context, session)
    assert retried.status_code == 202
    assert retried.json()["status"] == "sending"
    assert calls and calls[0]["local_document_uid"]
    assert calls[0]["cda"] and calls[0]["sig"] and calls[0]["patient"]["snils"]
    assert mock.module._register_calls == 1


def test_retryable_callback_codes_become_failed(client, remd):
    """Сбои РЭМД (429/503/500 в каталоге) — `failed`, а не терминальный `rejected` (P2)."""
    _, sink = remd
    for prefix, code, expected in (
        ("rd-rate", "RATE_LIMITED", "failed"),
        ("rd-mis", "MIS_NOT_AVAILABLE", "failed"),
        ("rd-internal", "INTERNAL_ERROR", "failed"),
        ("rd-invalid", "INVALID_SIGNATURE", "rejected"),
    ):
        session, context = _signed(client, prefix)
        assert _submit(client, context, session).status_code == 202
        request_id = _delivery(client, context, session).json()["request_id"]
        body = {
            "from_system_id": "remd-mock",
            "lpu_uid": settings.remd_lpu_uid,
            "response_to_request_id": request_id,
            "message_type": "registerDocumentResult",
            "status": "error",
            "errors": [{"code": code, "message": "сбой"}],
        }
        assert _callback(client, body).status_code == 200
        result = _delivery(client, context, session).json()
        assert result["status"] == expected, code
        assert result["last_error_code"] == code


def test_callback_with_foreign_lpu_is_rejected(client, remd):
    """Чужой lpu_uid не закрывает нашу доставку (P3)."""
    _, sink = remd
    session, context = _signed(client, "rd-lpu")
    assert _submit(client, context, session).status_code == 202
    payload = sink.wait(timeout=15)["body"]
    foreign = dict(payload, lpu_uid="1.2.643.5.1.13.13.12.2.99.9.9")
    rejected = _callback(client, foreign)
    assert rejected.status_code == 400
    assert _delivery(client, context, session).json()["status"] == "sending"


def test_forbidden_client_response_marks_failed(client, remd, monkeypatch):
    """403 (scope) — сбой контура, а не отказ документу: `failed`, ответ 502 (P2/P3)."""
    from app.remd.client import RemdClient, RemdRejected

    def deny(self, envelope):
        raise RemdRejected("INSUFFICIENT_SCOPE", "нет scope remd.write", 403)

    monkeypatch.setattr(RemdClient, "register_document", deny)
    session, context = _signed(client, "rd-403")
    assert _submit(client, context, session).status_code == 502
    result = _delivery(client, context, session).json()
    assert result["status"] == "failed"
    assert result["last_error_code"] == "INSUFFICIENT_SCOPE"


def test_submit_does_not_hold_protocol_lock_during_network(client, remd, db_session, monkeypatch):
    """Сеть РЭМД идёт вне транзакции: строка версии не заблокирована во время ответа (P1)."""
    import asyncio
    import threading
    import time as _time

    from app import db as db_module
    from app.remd import service
    from app.remd.client import RemdClient

    session, context = _signed(client, "rd-lock")
    protocol_id = db_session.execute(
        text(
            "SELECT id FROM session_protocols WHERE session_id = CAST(:s AS uuid) "
            "ORDER BY version DESC LIMIT 1"
        ).bindparams(s=session["id"])
    ).scalar_one()
    clinician_id = uuid.UUID(context["clinician_id"])
    session_id = uuid.UUID(session["id"])
    started, release = threading.Event(), threading.Event()

    def slow(self, envelope):
        started.set()
        release.wait(10)
        return "slow-request-id"

    monkeypatch.setattr(RemdClient, "register_document", slow)

    async def scenario():
        async with db_module.AsyncSessionLocal() as first, db_module.AsyncSessionLocal() as second:
            task = asyncio.create_task(service.submit(first, clinician_id, session_id))
            deadline = _time.monotonic() + 10
            while not started.is_set() and _time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert started.is_set(), "submit не дошёл до сети"
            began = _time.monotonic()
            await second.execute(
                text("UPDATE session_protocols SET updated_at = now() WHERE id = :p"),
                {"p": protocol_id},
            )
            await second.commit()
            waited = _time.monotonic() - began
            release.set()
            status, payload = await task
            return waited, status, payload

    waited, status, payload = asyncio.run(scenario())
    assert waited < 2.0, f"строка версии была заблокирована {waited:.1f} с"
    assert status == 202 and payload["status"] == "sending"
    assert payload["request_id"] == "slow-request-id"
