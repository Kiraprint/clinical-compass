"""FR-41: мок РЭМД — протокол обмена, подпись, идемпотентность, каталог ошибок.

Мок проверяется как отдельное приложение (docs/18 §8, research §2–§5): OAuth,
202 + асинхронный callback, криптографическая проверка отсоединённой CMS,
идемпотентность по `local_document_uid` и поведение клиента на 429/5xx.
"""

from __future__ import annotations

import base64
import json
import random
import urllib.parse
import urllib.request

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.errors import Problem
from app.remd import mock as remd_mock
from app.remd.client import RemdClient
from app.remd.config import RemdConfig

from .remd_helpers import SNILS, CallbackSink, MockRemd, envelope, make_cda, sign_cda


@pytest.fixture()
def api(monkeypatch):
    remd_mock.reset_state()
    monkeypatch.delenv("CC_REMD_MOCK_CALLBACK_URL", raising=False)
    monkeypatch.delenv("CC_REMD_MOCK_FORCE_STATUS", raising=False)
    monkeypatch.delenv("CC_REMD_MOCK_FAIL_FIRST", raising=False)
    return TestClient(remd_mock.app)


def _token(api) -> str:
    response = api.post(
        "/oauth/token",
        auth=("compass-lpu", "remd-secret"),
        data={"grant_type": "client_credentials"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["token_type"] == "Bearer"
    return response.json()["access_token"]


def _register(api, token: str, body: dict, **params):
    query = ("?" + urllib.parse.urlencode(params)) if params else ""
    return api.post(
        f"/remd/v1/register-document/{query}",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


def _client(
    url: str,
    *,
    attempts: int = 3,
    sleeps: list | None = None,
    retry_after_cap: float = 60.0,
) -> RemdClient:
    cfg = RemdConfig(
        enabled=True,
        base_url=url,
        client_id="compass-lpu",
        client_secret="remd-secret",
        lpu_uid="1.2.643.5.1.13.13.12.2.77.1.1",
        callback_url=f"{url}/callback",
        callback_token="test-token",
        allow_insecure=True,
        timeout=5,
        max_attempts=attempts,
        retry_after_cap=retry_after_cap,
    )
    return RemdClient(cfg, sleep=(sleeps.append if sleeps is not None else lambda _s: None),
                      rng=random.Random(0))


# --- OAuth -------------------------------------------------------------------


def test_token_requires_valid_credentials(api):
    bad = api.post(
        "/oauth/token",
        auth=("compass-lpu", "wrong"),
        data={"grant_type": "client_credentials"},
    )
    assert bad.status_code == 401
    assert bad.json()["errors"][0]["code"] == "UNAUTHORIZED"
    assert "access_token" not in bad.json()

    wrong_grant = api.post(
        "/oauth/token",
        auth=("compass-lpu", "remd-secret"),
        data={"grant_type": "password"},
    )
    assert wrong_grant.status_code == 400


def test_register_requires_bearer(api):
    response = api.post("/remd/v1/register-document/", json=envelope(callback_url=""))
    assert response.status_code == 401
    assert response.json()["errors"][0]["code"] == "UNAUTHORIZED"


# --- валидация конверта ------------------------------------------------------


def test_register_returns_202_and_request_id(api):
    token = _token(api)
    response = _register(api, token, envelope())
    assert response.status_code == 202, response.text
    request_id = response.json()["request_id"]
    assert request_id in remd_mock._registry


def test_snils_checksum_rejected(api):
    token = _token(api)
    response = _register(api, token, envelope(snils="112-233-445 96"))
    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "INVALID_SNILS"


def test_unknown_document_kind(api):
    token = _token(api)
    response = _register(api, token, envelope(kind=999))
    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "UNKNOWN_DOCUMENT_KIND"


def test_cda_must_be_cda_r2(api):
    token = _token(api)
    body = envelope()
    body["cda"] = base64.b64encode(b"not xml").decode()
    response = _register(api, token, body)
    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "VALIDATION_ERROR"


# --- подпись -----------------------------------------------------------------


def test_invalid_signature_rejected(api):
    token = _token(api)
    cda = make_cda()
    signature = sign_cda(cda)
    tampered = cda.replace(b"doc-1", b"doc-2")  # байты изменились после подписи
    body = envelope(cda=tampered, sig=signature)
    response = _register(api, token, body)
    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "INVALID_SIGNATURE"


def test_expired_certificate_rejected(api, monkeypatch):
    token = _token(api)
    monkeypatch.setattr(settings, "signing_cert_days", -1)
    from app.signing import ca

    cda = make_cda()
    signer = ca.issue_clinician("Иванов Иван Иванович", root=ca.authority().root)
    from app.signing import cms

    body = envelope(cda=cda, sig=cms.sign(cda, signer))
    response = _register(api, token, body)
    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "CERTIFICATE_EXPIRED"


def test_unknown_ca_rejected(api, tmp_path, monkeypatch):
    token = _token(api)
    from app.signing import ca, cms

    foreign = ca.authority(tmp_path / "foreign-ca")
    leaf = foreign.issue("clinician", ca.clinician_identity("Иванов Иван Иванович"), 30)
    cda = make_cda()
    body = envelope(cda=cda, sig=cms.sign(cda, leaf))
    response = _register(api, token, body)
    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "CERTIFICATE_NOT_FOUND"


# --- идемпотентность ---------------------------------------------------------


def test_same_uid_same_content_is_idempotent(api):
    token = _token(api)
    body = envelope(callback_url="")
    first = _register(api, token, body)
    second = _register(api, token, dict(body))
    assert first.status_code == second.status_code == 202
    assert first.json()["request_id"] == second.json()["request_id"]
    assert len(remd_mock._registry) == 1


def test_same_uid_other_content_conflicts(api):
    token = _token(api)
    first = envelope(callback_url="", number="CC-a")
    assert _register(api, token, first).status_code == 202
    other_cda = make_cda("doc-2")
    second = envelope(cda=other_cda, callback_url="", number="CC-b")
    response = _register(api, token, second)
    assert response.status_code == 409
    assert response.json()["errors"][0]["code"] == "NOT_UNIQUE_PROVIDED_ID"


def test_duplicate_document_number_conflicts(api):
    token = _token(api)
    assert _register(api, token, envelope(callback_url="", number="CC-dup")).status_code == 202
    other = envelope(callback_url="", number="CC-dup",
                     local_uid="99999999-2222-3333-4444-555555555555")
    response = _register(api, token, other)
    assert response.status_code == 409
    assert response.json()["errors"][0]["code"] == "DUPLICATE_DOCUMENT"


# --- инъекция сбоев и клиент -------------------------------------------------


def test_simulate_500_and_429(api):
    token = _token(api)
    assert _register(api, token, envelope(), simulate="500").status_code == 500
    throttled = _register(api, token, envelope(), simulate="429")
    assert throttled.status_code == 429
    assert throttled.headers["Retry-After"] == "1"
    assert throttled.json()["errors"][0]["code"] == "RATE_LIMITED"


def test_client_retries_500_then_succeeds(monkeypatch):
    sleeps: list[float] = []
    with MockRemd() as remd:
        monkeypatch.setenv("CC_REMD_MOCK_FAIL_FIRST", "1")
        request_id = _client(remd.url, sleeps=sleeps).register_document(envelope())
        assert request_id in remd.registry
        assert len(sleeps) == 1


def test_client_gives_up_after_max_attempts(monkeypatch):
    sleeps: list[float] = []
    with MockRemd() as remd:
        monkeypatch.setenv("CC_REMD_MOCK_FORCE_STATUS", "500")
        with pytest.raises(Problem) as exc:
            _client(remd.url, attempts=3, sleeps=sleeps).register_document(envelope())
        assert exc.value.status == 502 and exc.value.code == "EMIAS_UNAVAILABLE"
        assert len(sleeps) == 2  # между тремя попытками


def test_client_honours_retry_after(monkeypatch):
    sleeps: list[float] = []
    with MockRemd() as remd:
        monkeypatch.setenv("CC_REMD_MOCK_FORCE_STATUS", "429")
        monkeypatch.setenv("CC_REMD_MOCK_RETRY_AFTER", "0")
        with pytest.raises(Problem):
            _client(remd.url, attempts=2, sleeps=sleeps).register_document(envelope())
        assert sleeps == [0.0]


# --- callback ----------------------------------------------------------------


def test_callback_delivered_and_retry_hook(monkeypatch):
    """At-least-once: недоставленный callback повторяется хуком и меняет статус."""
    with MockRemd() as remd, CallbackSink(status=503) as sink:
        monkeypatch.setenv("CC_REMD_MOCK_CALLBACK_URL", sink.url)
        monkeypatch.setenv("CC_REMD_MOCK_CALLBACK_ATTEMPTS", "2")
        client = _client(remd.url)
        request_id = client.register_document(envelope())
        assert len(sink.wait_calls(2, timeout=10)) == 2  # все попытки упали
        entry = next(e for e in remd.registry.values() if e["request_id"] == request_id)
        assert entry["status"] == "VALIDATING"

        sink.status = 200
        token = remd.token()

        request = urllib.request.Request(
            f"{remd.url}/remd/v1/_test/callback-retry/{request_id}",
            data=b"",
            method="POST",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=10) as reply:
            assert json.loads(reply.read())["delivered"] is True
        assert entry["status"] == "REGISTERED"
        payload = sink.calls[-1]["body"]
        assert payload["message_type"] == "registerDocumentResult"
        assert payload["status"] == "success"
        assert payload["data"]["emdr_id"] == entry["emdr_id"]


def test_registry_lookup(api):
    token = _token(api)
    request_id = _register(api, token, envelope()).json()["request_id"]
    emdr_id = remd_mock._registry[request_id]["emdr_id"]
    item = api.get(f"/remd/v1/registry/{emdr_id}")
    assert item.status_code == 200
    assert item.json()["status"] == "VALIDATING"
    assert api.get("/remd/v1/registry/nope").status_code == 404


def test_metadata_request_is_not_a_registry_item(api):
    """get-metadata не должен выглядеть зарегистрированным документом в реестре."""
    token = _token(api)
    request_id = _register(api, token, envelope()).json()["request_id"]
    emdr_id = remd_mock._registry[request_id]["emdr_id"]

    reply = api.post(
        "/remd/v1/get-metadata/",
        json={"lpu_uid": "1.2.643.5.1.13.13.12.2.77.1.1", "emdr_id": emdr_id},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert reply.status_code == 202
    assert reply.json()["request_id"] not in remd_mock._registry
    assert reply.json()["request_id"] in remd_mock._requests
    assert api.get(f"/remd/v1/registry/{emdr_id}").status_code == 200


def test_client_caps_retry_after(monkeypatch):
    """Retry-After от шлюза не может остановить submit на часы (P1)."""
    sleeps: list[float] = []
    with MockRemd() as remd:
        monkeypatch.setenv("CC_REMD_MOCK_FORCE_STATUS", "429")
        monkeypatch.setenv("CC_REMD_MOCK_RETRY_AFTER", "86400")
        with pytest.raises(Problem):
            _client(remd.url, attempts=2, sleeps=sleeps, retry_after_cap=2.0).register_document(
                envelope()
            )
        assert sleeps == [2.0]


def test_register_requires_submit_scope(api):
    """Шаблон scope → 403 из мока ЕМИАС: без remd.write регистрации нет."""
    narrow = api.post(
        "/oauth/token",
        auth=("compass-lpu", "remd-secret"),
        data={"grant_type": "client_credentials", "scope": "registry.read"},
    )
    assert narrow.status_code == 200
    assert narrow.json()["scope"] == "registry.read"
    denied = _register(api, narrow.json()["access_token"], envelope())
    assert denied.status_code == 403
    assert denied.json()["errors"][0]["code"] == "INSUFFICIENT_SCOPE"


def test_callback_retry_does_not_downgrade_registered(monkeypatch):
    """Повторная доставка не может снять терминальный REGISTERED."""
    with MockRemd() as remd, CallbackSink() as sink:
        monkeypatch.setenv("CC_REMD_MOCK_CALLBACK_URL", sink.url)
        request_id = _client(remd.url).register_document(envelope())
        sink.wait_calls(1, timeout=10)
        assert remd.registry[request_id]["status"] == "REGISTERED"

        sink.status = 500
        retry = urllib.request.Request(
            f"{remd.url}/remd/v1/_test/callback-retry/{request_id}",
            method="POST",
            headers={"Authorization": f"Bearer {remd.token()}"},
        )
        with urllib.request.urlopen(retry, timeout=10) as response:
            assert response.status == 200
        assert sink.wait_calls(2, timeout=10)
        assert remd.registry[request_id]["status"] == "REGISTERED"
