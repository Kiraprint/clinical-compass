# Item 3: уведомления врачу при RED-эскалации + Web Push подписки.
# Контракт: docs/04 §3.1; docs/06 §3.
from .conftest import admin_ctx, auth_header, clinician_ctx, finish_screening, patient_ctx

# q9 (суицидальный пункт PHQ-9) >= 1 → SUICIDAL_IDEATION → RED
RED_ANSWERS = {f"q{i}": 0 for i in range(1, 10)}
RED_ANSWERS["q9"] = 1


def _grant(client, admin_h, patient_id, clinician_id) -> None:
    resp = client.post(
        "/api/v1/admin/roster", json={"patient_id": patient_id, "clinician_id": clinician_id}, headers=admin_h
    )
    assert resp.status_code == 201, resp.text


def test_red_escalation_notifies_rostered_clinician(client):
    """RED-скрининг создаёт Notification врачу из roster (в транзакции finish)."""
    h, pid = patient_ctx(client, "pn@test.ru")
    c, cid = clinician_ctx(client, "cn@test.ru")
    a, _ = admin_ctx(client, "an@test.ru")
    _grant(client, a, pid, cid)

    assessment = finish_screening(client, h, RED_ANSWERS)
    assert assessment["triage"] == "RED"

    resp = client.get("/api/v1/me/notifications", headers=c)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1, items
    n = items[0]
    assert n["kind"] == "RED_ESCALATION"
    assert n["patient_id"] == str(pid)
    assert n["read"] is False
    assert resp.json()["unread"] == 1
    assert n["escalation_id"] is not None


def test_red_escalation_no_roster_no_notification(client):
    """Без roster пациент врач не уведомляется (и чужие уведомления не видны)."""
    h, _ = patient_ctx(client, "pnx@test.ru")
    c, _ = clinician_ctx(client, "cnx@test.ru")
    finish_screening(client, h, RED_ANSWERS)

    resp = client.get("/api/v1/me/notifications", headers=c)
    assert resp.status_code == 200
    assert resp.json()["items"] == []
    assert resp.json()["unread"] == 0


def test_notifications_mark_read_individual(client):
    h, pid = patient_ctx(client, "prd@test.ru")
    c, cid = clinician_ctx(client, "crd@test.ru")
    a, _ = admin_ctx(client, "ard@test.ru")
    _grant(client, a, pid, cid)
    finish_screening(client, h, RED_ANSWERS)

    items = client.get("/api/v1/me/notifications", headers=c).json()["items"]
    nid = items[0]["id"]
    resp = client.post("/api/v1/me/notifications/read", json={"ids": [nid]}, headers=c)
    assert resp.status_code == 200

    after = client.get("/api/v1/me/notifications", headers=c).json()
    assert after["items"][0]["read"] is True
    assert after["unread"] == 0


def test_push_subscribe_unsubscribe_and_vapid_key(client):
    c, _ = clinician_ctx(client, "csub@test.ru")
    key_resp = client.get("/api/v1/push/vapid-public-key", headers=c)
    assert key_resp.status_code == 200
    pub = key_resp.json()["public_key"]
    assert len(pub) > 40  # base64url-точка EC P-256 (65 байт)

    # без токена — 401
    assert client.get("/api/v1/push/vapid-public-key").status_code == 401

    endpoint = "https://push.example.test/some-endpoint"
    body = {
        "endpoint": endpoint,
        "keys": {"p256dh": "x" * 87, "auth": "y" * 22},
    }
    assert client.post("/api/v1/push/subscribe", json=body, headers=c).status_code == 200
    # идемпотентный апсерт: повторная подписка не падает
    assert client.post("/api/v1/push/subscribe", json={**body, "keys": {"p256dh": "z" * 87, "auth": "w" * 22}}, headers=c).status_code == 200
    # чужая подписка не удаляется
    p, _ = patient_ctx(client, "psub@test.ru")
    assert client.request("DELETE", "/api/v1/push/unsubscribe", json={"endpoint": endpoint}, headers=p).status_code == 200
    # своя — удаляется
    assert client.request("DELETE", "/api/v1/push/unsubscribe", json={"endpoint": endpoint}, headers=c).status_code == 200
    assert client.request("DELETE", "/api/v1/push/unsubscribe", json={"endpoint": endpoint}, headers=c).status_code == 200