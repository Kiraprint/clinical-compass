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