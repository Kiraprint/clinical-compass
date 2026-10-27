# Item 3: уведомления врачу при RED-эскалации + Web Push подписки.
# Item 4: FCM-токены нативного приложения + cooldown-окно апгрейда.
# Контракт: docs/04 §3.1; docs/06 §3.
import time

from sqlalchemy import text

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
# --- Item 4: FCM-токены нативного приложения + cooldown -----------------------

def test_fcm_device_token_register_delete(client):
    """Регистрация FCM-токена идемпотентна; удаление — scoped по владельцу."""
    h, pid = patient_ctx(client, "fcm@test.ru")
    body = {"token": "fcm-token-abc", "platform": "android"}
    assert client.post("/api/v1/push/device-token", json=body, headers=h).status_code == 200
    # идемпотентный апсерт
    assert client.post("/api/v1/push/device-token", json=body, headers=h).status_code == 200
    # чужая запись не удаляется
    p2, _ = patient_ctx(client, "fcm2@test.ru")
    assert client.request("DELETE", "/api/v1/push/device-token", json=body, headers=p2).status_code == 200
    # своя — удаляется
    assert client.request("DELETE", "/api/v1/push/device-token", json=body, headers=h).status_code == 200
    # без токена — 401
    assert client.get("/api/v1/push/fcm/status").status_code == 401

def test_fcm_upgrade_then_cooldown(client):
    """После успешного апгрейда повторная подписка — 429 FCM_COOLDOWN (окно 60 мин)."""
    h, pid = patient_ctx(client, "fcmup@test.ru")
    body = {"token": "fcm-upgrade-tok", "platform": "android"}

    # до апгрейда окно не активно (0 сек)
    st = client.get("/api/v1/push/fcm/status", headers=h)
    assert st.status_code == 200
    assert st.json() == {"window_active": False, "remaining_seconds": 0}

    # первый апгрейд — успех
    up = client.post("/api/v1/push/fcm/upgrade", json=body, headers=h)
    assert up.status_code == 200, up.text
    assert up.json() == {"upgraded": True}

    # окно теперь активно — повторный апгрейд → 429 FCM_COOLDOWN
    st = client.get("/api/v1/push/fcm/status", headers=h).json()
    assert st["window_active"] is True
    assert st["remaining_seconds"] > 0
    second = client.post("/api/v1/push/fcm/upgrade", json=body, headers=h)
    assert second.status_code == 429, second.text
    assert second.json()["code"] == "FCM_COOLDOWN"

# --- FR-10: сообщение пациента в чате уведомляет врача ------------------------

def _send_chat(client, h, text: str) -> None:
    resp = client.post("/api/v1/chat/thread", json={"content": text}, headers=h)
    assert resp.status_code == 201, resp.text

def test_patient_message_notifies_rostered_clinician_without_text(client):
    """Сообщение пациента → Notification врачу roster; тело без текста, пациенту — ничего."""
    h, pid = patient_ctx(client, "pm@test.ru")
    c, cid = clinician_ctx(client, "pmc@test.ru")
    a, _ = admin_ctx(client, "pma@test.ru")
    _grant(client, a, pid, cid)

    text = "уникальный текст сообщения 42"
    _send_chat(client, h, text)

    items = client.get("/api/v1/me/notifications", headers=c).json()["items"]
    assert len(items) == 1, items
    n = items[0]
    assert n["kind"] == "PATIENT_MESSAGE"
    assert n["patient_id"] == str(pid)
    assert n["read"] is False
    assert text not in n["body"] and text not in n["title"]
    # Пациент без ФИО: заголовок нейтральный. Логин (e-mail) в заголовок не попадает
    # никогда — иначе он уезжал бы в push-превью на заблокированном экране.
    assert n["title"] == "Новое сообщение в чате", n["title"]
    assert "pm" not in n["title"]

    # Отправитель уведомление о своём сообщении не получает.
    own = client.get("/api/v1/me/notifications", headers=h).json()
    assert own["items"] == []

def test_patient_messages_dedup_until_read(client):
    """До прочтения — одна строка на (врач, пациент); created_at освежается."""
    h, pid = patient_ctx(client, "pmd@test.ru")
    c, cid = clinician_ctx(client, "pmdc@test.ru")
    a, _ = admin_ctx(client, "pmda@test.ru")
    _grant(client, a, pid, cid)

    _send_chat(client, h, "первое")
    first = client.get("/api/v1/me/notifications", headers=c).json()["items"]
    assert len(first) == 1
    time.sleep(0.02)
    _send_chat(client, h, "второе")

    after = client.get("/api/v1/me/notifications", headers=c).json()
    assert len(after["items"]) == 1, after["items"]
    assert after["items"][0]["created_at"] > first[0]["created_at"]

    # После прочтения новое сообщение снова создаёт строку.
    assert client.post("/api/v1/me/notifications/read", json={"ids": None}, headers=c).status_code == 200
    _send_chat(client, h, "третье")
    fresh = client.get("/api/v1/me/notifications", headers=c).json()
    assert len(fresh["items"]) == 2, fresh["items"]
    assert fresh["unread"] == 1

def test_unread_counts_real_number(client):
    """`unread` — count(*) непрочитанных, а не 0/1."""
    c, cid = clinician_ctx(client, "pmu@test.ru")
    a, _ = admin_ctx(client, "pmua@test.ru")
    for i in (1, 2):
        h, pid = patient_ctx(client, f"pmu{i}@test.ru")
        _grant(client, a, pid, cid)
        _send_chat(client, h, f"сообщение {i}")

    data = client.get("/api/v1/me/notifications", headers=c).json()
    assert len(data["items"]) == 2
    assert sum(1 for n in data["items"] if not n["read"]) == 2
    assert data["unread"] == 2


def test_notification_failure_does_not_lose_patient_message(client, monkeypatch):
    """Сбой уведомления/пуша не откатывает само сообщение пациенту."""
    from app.services import push as push_service

    h, pid = patient_ctx(client, "pmf@test.ru")
    c, cid = clinician_ctx(client, "pmfc@test.ru")
    a, _ = admin_ctx(client, "pmfa@test.ru")
    _grant(client, a, pid, cid)

    async def boom(db, patient_id):
        raise RuntimeError("push boom")

    monkeypatch.setattr(push_service, "notify_patient_message", boom)
    text = "сообщение при сбое уведомления"
    _send_chat(client, h, text)

    thread = client.get("/api/v1/chat/thread", headers=h).json()["items"]
    assert any(text in m["content"] for m in thread), thread
    assert client.get("/api/v1/me/notifications", headers=c).json()["items"] == []


def test_patient_message_title_uses_fio_only(client, db_session):
    """ФИО пациента (обрезанное) — в заголовке; ни e-mail, ни текста сообщения там нет."""
    h, pid = patient_ctx(client, "pmt@test.ru")
    c, cid = clinician_ctx(client, "pmtc@test.ru")
    a, _ = admin_ctx(client, "pmta@test.ru")
    _grant(client, a, pid, cid)
    db_session.execute(
        text("update users set fio = :f where id = :p"),
        {"f": "  Иванов Иван Иванович  ", "p": pid},
    )
    db_session.commit()

    _send_chat(client, h, "текст который не должен утечь")

    n = client.get("/api/v1/me/notifications", headers=c).json()["items"][0]
    assert n["title"] == "Новое сообщение от Иванов Иван Иванович"
    assert "pmt@test.ru" not in n["title"] and "pmt@test.ru" not in n["body"]
    assert "не должен утечь" not in n["body"]
