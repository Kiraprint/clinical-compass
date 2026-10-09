# Контракт: docs/04 §3.1; docs/06 §3 (consent_gating, consent_revoke)
import pyotp

from .conftest import auth_header, login, register


def test_consent_create_get_revoke_cycle(client):
    register(client, "cs@test.ru")
    data = login(client, "cs@test.ru")
    h = auth_header(data["access_token"])

    resp = client.post("/api/v1/consent", json={"version": 1}, headers=h)
    assert resp.status_code == 201
    assert resp.json()["revoked_at"] is None

    assert client.get("/api/v1/consent", headers=h).status_code == 200

    # Повторное подписание при активном согласии → 409
    resp = client.post("/api/v1/consent", json={"version": 1}, headers=h)
    assert resp.status_code == 409
    assert resp.json()["code"] == "CONFLICT"

    assert client.post("/api/v1/consent/revoke", headers=h).status_code == 204
    assert client.get("/api/v1/consent", headers=h).status_code == 404

    # После отзыва можно подписать снова
    resp = client.post("/api/v1/consent", json={"version": 2}, headers=h)
    assert resp.status_code == 201


def test_consent_requires_patient_role(client):
    reg = register(client, "csdoc@test.ru", role="CLINICIAN")
    data = login(client, "csdoc@test.ru", otp=pyotp.TOTP(reg["totp_secret"]).now())
    resp = client.post("/api/v1/consent", json={"version": 1}, headers=auth_header(data["access_token"]))
    assert resp.status_code == 403
    assert resp.json()["code"] == "ROLE_MISMATCH"