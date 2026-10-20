# Контракт: docs/04 §3.1; docs/06 §3 (consent_gating, consent_revoke)
import pyotp

from .conftest import auth_header, finish_screening, login, patient_ctx, register


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


def test_consent_revoke_destroys_patient_pdn(client, db_session):
    """F2 (152-ФЗ ст.9 ч.2-3): отзыв согласия уничтожает спецкатегорию ПДн."""
    from sqlalchemy import func, select

    from app.models import (
        AdherenceRecord,
        Answer,
        Assessment,
        ChatMessage,
        Consent,
        DiaryEntry,
        Run,
    )
    from .conftest import finish_screening

    h, pid = patient_ctx(client, "f2pdn@test.ru")
    # наполняем клиническими данными
    assert client.post("/api/v1/diary", json={"content": "тревожно", "mood": 2}, headers=h).status_code == 201
    finish_screening(client, h, {f"q{i}": 1 for i in range(1, 10)})
    assert client.post("/api/v1/me/adherence", json={"medication": "Авиандр", "status": "taken"}, headers=h).status_code == 201
    assert client.post("/api/v1/chat/thread", json={"content": "помогите"}, headers=h).status_code == 201

    def _count(model):
        return db_session.execute(select(func.count()).select_from(model).where(model.patient_id == pid)).scalar_one()

    assert _count(DiaryEntry) == 1
    assert _count(Run) >= 1
    assert _count(Assessment) == 1
    assert _count(AdherenceRecord) == 1
    assert _count(ChatMessage) >= 1  # patient + SYSTEM auto-reply

    # отзыв согласия → уничтожение
    assert client.post("/api/v1/consent/revoke", headers=h).status_code == 204

    # вся спецкатегория ПДн удалена
    assert _count(DiaryEntry) == 0
    assert _count(Run) == 0
    assert _count(Assessment) == 0
    assert _count(AdherenceRecord) == 0
    assert _count(ChatMessage) == 0
    # answers удалились вместе с runs (нет patient_id — проверяем через подзапрос)
    ans_left = db_session.execute(
        select(func.count()).select_from(Answer).where(Answer.run_id.in_(select(Run.id).where(Run.patient_id == pid)))
    ).scalar_one()
    assert ans_left == 0

    # consent остаётся (реквизиты ст. 9 ч.4 — основание обработки), revoked_at проставлен
    consent = db_session.execute(select(Consent).where(Consent.user_id == pid)).scalar_one()
    assert consent.revoked_at is not None


def test_consent_revoke_destroys_push_tokens(client, db_session):
    """F2 re-audit: PushSubscription/DeviceToken уничтожаются при отзыве согласия
    (канал доставки — иначе отозванный пациент продолжает получать push)."""
    from sqlalchemy import func, select

    from app.models import DeviceToken, PushSubscription

    h, pid = patient_ctx(client, "f2push@test.ru")
    # наполняем каналами доставки push
    db_session.add(PushSubscription(
        user_id=pid, endpoint="https://push.example.com/sub/abc",
        p256dh="key-p256dh", auth="key-auth",
    ))
    db_session.add(DeviceToken(user_id=pid, token="fcm-token-xyz", platform="android"))
    db_session.commit()

    def _count(model):
        return db_session.execute(select(func.count()).select_from(model).where(model.user_id == pid)).scalar_one()

    assert _count(PushSubscription) == 1
    assert _count(DeviceToken) == 1

    # отзыв согласия → уничтожение каналов доставки
    assert client.post("/api/v1/consent/revoke", headers=h).status_code == 204

    assert _count(PushSubscription) == 0
    assert _count(DeviceToken) == 0


def test_push_blocked_after_consent_revoke(client):
    """F2 round-3: после отзыва согласия пациент не может восстановить каналы
    доставки push (иначе _destroy_patient_pdn бесполезен)."""
    h, _pid = patient_ctx(client, "f2pushgate@test.ru")
    # до отзыва — push-эндпоинты доступны
    assert client.get("/api/v1/push/vapid-public-key", headers=h).status_code == 200
    assert client.post("/api/v1/consent/revoke", headers=h).status_code == 204
    # после отзыва — 403 CONSENT_REQUIRED на всех push/notification эндпоинтах
    for method, path, body in (
        ("get", "/api/v1/push/vapid-public-key", None),
        ("post", "/api/v1/push/subscribe", {"endpoint": "https://x/y", "p256dh": "k", "auth": "a"}),
        ("post", "/api/v1/push/device-token", {"token": "t", "platform": "android"}),
        ("get", "/api/v1/push/fcm/status", None),
        ("get", "/api/v1/me/notifications", None),
    ):
        r = getattr(client, method)(path, **({"json": body} if body else {}), headers=h)
        assert r.status_code == 403, f"{method} {path} → {r.status_code}"
        assert r.json()["code"] == "CONSENT_REQUIRED"


def test_clinician_audit_records_client_ip(client, db_session):
    """F6 round-3: действия врача аудируются с ip (--proxy-headers → request.client)."""
    from sqlalchemy import select

    from app.models import AuditLog

    from .conftest import admin_ctx, clinician_ctx

    h, cid = clinician_ctx(client, "f6ip@test.ru")
    _hp, pid = patient_ctx(client, "f6ippat@test.ru")
    # ростер: врач получает пациента
    ha, _aid = admin_ctx(client, "f6ipadmin@test.ru")
    assert client.post(
        "/api/v1/admin/roster",
        json={"clinician_id": cid, "patient_id": pid},
        headers=ha,
    ).status_code in (201, 200)
    assert client.get(f"/api/v1/clinician/patients/{pid}/report", headers=h).status_code == 200
    row = db_session.execute(
        select(AuditLog).where(AuditLog.action == "READ_REPORT").order_by(AuditLog.occurred_at.desc())
    ).scalars().first()
    assert row is not None and row.ip, "READ_REPORT должен содержать ip"


def test_consent_revoke_destroys_visits(client, db_session):
    """F2 (152-ФЗ ст.9 ч.2-3): отзыв согласия уничтожает записи приёма врача.

    notes приёма — зашифрованный клинический текст (спецкатегория ПДн); после
    отзыва согласия строка Visit должна исчезнуть из БД, иначе врач сохраняет
    доступ к заметкам беседы.
    """
    from sqlalchemy import func, select

    from app.models import Visit

    from .conftest import admin_ctx, clinician_ctx

    h, pid = patient_ctx(client, "f2visit@test.ru")
    c, cid = clinician_ctx(client, "f2visitc@test.ru")
    ha, _aid = admin_ctx(client, "f2visita@test.ru")
    assert client.post(
        "/api/v1/admin/roster",
        json={"clinician_id": cid, "patient_id": pid},
        headers=ha,
    ).status_code in (201, 200)

    resp = client.post(
        f"/api/v1/clinician/patients/{pid}/visits",
        json={
            "visited_at": "2026-10-18T12:00:00+03:00",
            "visit_type": "online",
            "duration_min": 50,
            "notes": "Обсудили динамику тревоги, план — продолжить терапию.",
        },
        headers=c,
    )
    assert resp.status_code == 201, resp.text

    def _count():
        return db_session.execute(
            select(func.count()).select_from(Visit).where(Visit.patient_id == pid)
        ).scalar_one()

    assert _count() == 1

    # отзыв согласия → уничтожение записи приёма
    assert client.post("/api/v1/consent/revoke", headers=h).status_code == 204
    db_session.expire_all()
    assert _count() == 0
