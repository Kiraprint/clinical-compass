"""FR-49: приглашение пациента врачом — карта, ссылка, активация, ростер.

Проверки состояния идут через async-ORM (AsyncSessionLocal), а не через
синхронный engine: приложение целиком на async SQLAlchemy (docs/03 §12), и
тест не должен проверять его другим драйвером.

Контракт: docs/04 FR-49, docs/03 §16, docs/06 §3, docs/10 R33.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, ClinicianRoster, PatientInvite, User

from .conftest import clinician_ctx, patient_ctx, run_async

PASS = "Str0ng-Pass!"


def _clinician(client, tag: str) -> tuple[dict, str]:
    return clinician_ctx(client, f"inv{tag}@test.ru")


def _create(client, headers, **kw) -> dict:
    body: dict = {"fio": kw.get("fio", "Петров Пётр Петрович")}
    if kw.get("phone"):
        body["phone"] = kw["phone"]
    if kw.get("birth_date"):
        body["birth_date"] = kw["birth_date"]
    if kw.get("email"):
        body["email"] = kw["email"]
    resp = client.post("/api/v1/clinician/patients/invites", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _accept(client, token, *, email=None, password=PASS) -> dict:
    body: dict = {"password": password}
    if email is not None:
        body["email"] = email
    resp = client.post(f"/api/v1/auth/invite/{token}/accept", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _patient(
    session: AsyncSession, email: str
) -> tuple[User, ClinicianRoster | None]:
    user = await session.scalar(select(User).where(User.email == email))
    assert user is not None, "аккаунт пациента не создан"
    roster = await session.scalar(
        select(ClinicianRoster).where(ClinicianRoster.patient_id == user.id)
    )
    return user, roster


async def _actions(session: AsyncSession) -> list[str]:
    rows = await session.scalars(
        select(AuditLog.action).where(AuditLog.action.like("INVITE_%")).order_by(AuditLog.id)
    )
    return list(rows)


async def _expire(session: AsyncSession, invite_id: str) -> None:
    await session.execute(
        update(PatientInvite)
        .where(PatientInvite.id == invite_id)
        .values(expires_at=datetime.now(timezone.utc) - timedelta(days=1))
    )
    await session.commit()


def test_create_lists_and_masks_phone(client):
    """Создание отдаёт ссылку один раз; список показывает карту без токена."""
    h, _ = _clinician(client, "a")
    invite = _create(client, h, phone="89261234567", birth_date="1990-05-05")
    assert invite["token"] and invite["link"].startswith("/invite/")
    assert invite["phone"] == "+7•••123-45-67"  # маска, а не номер
    assert invite["birth_date"] == "1990-05-05"

    resp = client.get("/api/v1/clinician/patients/invites", headers=h)
    assert resp.status_code == 200, resp.text
    (row,) = resp.json()["items"]
    assert row["id"] == invite["id"]
    assert "token" not in row and "link" not in row  # ссылку повторно не показать


def test_accept_creates_account_and_roster(client):
    """Активация: аккаунт пациента, закрепление за врачом, сессия, аудит."""
    h, cid = _clinician(client, "b")
    invite = _create(client, h, phone="89261234567")
    token = invite["token"]

    got = client.get(f"/api/v1/auth/invite/{token}")
    assert got.status_code == 200, got.text
    assert got.json()["fio"] == "Петров Пётр Петрович"

    data = _accept(client, token, email="invited@test.ru")
    assert data["role"] == "PATIENT" and data["access_token"] and data["refresh_token"]

    user, roster = run_async(_patient, "invited@test.ru")
    assert user.role == "PATIENT"
    assert user.fio == "Петров Пётр Петрович"
    assert user.phone == "+79261234567"  # EncryptedText расшифрован ORM
    # Ростер создан автоматически — без ручного /admin/roster.
    assert roster is not None and str(roster.clinician_id) == cid
    assert run_async(_actions) == ["INVITE_CREATE", "INVITE_ACCEPT"]

    # Ссылка одноразовая: повторные GET/accept — 410.
    assert client.get(f"/api/v1/auth/invite/{token}").status_code == 410
    assert client.post(
        f"/api/v1/auth/invite/{token}/accept", json={"password": PASS}
    ).status_code == 410


def test_accept_with_prefilled_email_and_password_policy(client):
    """E-mail врача становится логином; пароль короче 8 — 422."""
    h, _ = _clinician(client, "c")
    invite = _create(client, h, email="known@test.ru")
    assert _accept(client, invite["token"])["role"] == "PATIENT"
    user, _ = run_async(_patient, "known@test.ru")
    assert user.email == "known@test.ru"

    invite2 = _create(client, h, email="known2@test.ru")
    resp = client.post(
        f"/api/v1/auth/invite/{invite2['token']}/accept",
        json={"email": "known2@test.ru", "password": "short"},
    )
    assert resp.status_code == 422


def test_email_taken_409_and_unknown_token_404(client):
    h, _ = _clinician(client, "d")
    patient_ctx(client, "existing@test.ru")  # адрес уже занят
    invite = _create(client, h, email="existing@test.ru")
    resp = client.post(
        f"/api/v1/auth/invite/{invite['token']}/accept", json={"password": PASS}
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "EMAIL_TAKEN"
    assert client.get("/api/v1/auth/invite/unknown-token").status_code == 404


def test_expired_token_410(client):
    h, _ = _clinician(client, "e")
    invite = _create(client, h)
    run_async(_expire, invite["id"])
    assert client.get(f"/api/v1/auth/invite/{invite['token']}").status_code == 410
    assert client.post(
        f"/api/v1/auth/invite/{invite['token']}/accept", json={"password": PASS}
    ).status_code == 410


def test_reissue_kills_old_link_and_is_owner_scoped(client):
    """Перевыпуск гасит старую ссылку; чужая карта — 404 у другого врача."""
    h1, _ = _clinician(client, "f")
    h2, _ = _clinician(client, "g")
    invite = _create(client, h1)
    old = invite["token"]

    assert client.get("/api/v1/clinician/patients/invites", headers=h2).json()["items"] == []
    assert client.post(
        f"/api/v1/clinician/patients/invites/{invite['id']}/reissue", headers=h2
    ).status_code == 404
    assert client.delete(
        f"/api/v1/clinician/patients/invites/{invite['id']}", headers=h2
    ).status_code == 404

    resp = client.post(
        f"/api/v1/clinician/patients/invites/{invite['id']}/reissue", headers=h1
    )
    assert resp.status_code == 201, resp.text
    new_token = resp.json()["token"]
    assert new_token != old
    assert client.get(f"/api/v1/auth/invite/{old}").status_code == 404
    assert client.get(f"/api/v1/auth/invite/{new_token}").status_code == 200


def test_revoke_hides_invite(client):
    h, _ = _clinician(client, "h")
    invite = _create(client, h)
    assert client.delete(
        f"/api/v1/clinician/patients/invites/{invite['id']}", headers=h
    ).status_code == 204
    assert client.get("/api/v1/clinician/patients/invites", headers=h).json()["items"] == []
    assert client.get(f"/api/v1/auth/invite/{invite['token']}").status_code == 404


def test_accepted_invite_cannot_be_reissued(client):
    h, _ = _clinician(client, "i")
    invite = _create(client, h)
    _accept(client, invite["token"], email="used@test.ru")
    resp = client.post(
        f"/api/v1/clinician/patients/invites/{invite['id']}/reissue", headers=h
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "INVITE_ACCEPTED"


def test_invalid_fields_422(client):
    h, _ = _clinician(client, "j")
    assert client.post(
        "/api/v1/clinician/patients/invites",
        json={"fio": "X", "phone": "not-a-phone"},
        headers=h,
    ).status_code == 422
    assert client.post(
        "/api/v1/clinician/patients/invites",
        json={"fio": "X", "birth_date": "1500-01-01"},
        headers=h,
    ).status_code == 422
    assert client.post(
        "/api/v1/clinician/patients/invites",
        json={"fio": "X", "email": "no-at-sign"},
        headers=h,
    ).status_code == 422


def test_invite_limit_409(client):
    h, _ = _clinician(client, "k")
    from app.services.invites import MAX_PENDING

    for i in range(MAX_PENDING):
        _create(client, h, fio=f"Пациент {i}")
    resp = client.post(
        "/api/v1/clinician/patients/invites", json={"fio": "Лишний"}, headers=h
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "INVITE_LIMIT"


def test_invite_routes_require_auth(client):
    assert client.get("/api/v1/clinician/patients/invites").status_code == 401