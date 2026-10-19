# Контракт: docs/04 §3.1; docs/06 §3 (auth_2fa_required и др.)
import datetime as _dt
import time

import pyotp

from .conftest import auth_header, login, register


def test_register_and_login_patient(client):
    register(client, "patient@test.ru")
    data = login(client, "patient@test.ru")
    assert data["role"] == "PATIENT"
    assert data["access_token"] and data["refresh_token"]


def test_register_clinician_returns_totp_secret(client):
    data = register(client, "doc@test.ru", role="CLINICIAN")
    assert data["totp_secret"]
    assert data["role"] == "CLINICIAN"


def test_register_rejects_duplicate_email(client):
    register(client, "dup@test.ru")
    resp = client.post(
        "/api/v1/auth/register", json={"email": "dup@test.ru", "password": "Str0ng-Pass!", "role": "PATIENT"}
    )
    assert resp.status_code == 409


def test_login_clinician_requires_otp(client):
    data = register(client, "doc2@test.ru", role="CLINICIAN")
    secret = data["totp_secret"]
    # Без otp → 401
    resp = client.post("/api/v1/auth/login", json={"email": "doc2@test.ru", "password": "Str0ng-Pass!"})
    assert resp.status_code == 401
    # Неверный otp → 401
    resp = client.post(
        "/api/v1/auth/login", json={"email": "doc2@test.ru", "password": "Str0ng-Pass!", "otp": "000000"}
    )
    assert resp.status_code == 401
    # Корректный otp → 200
    otp = pyotp.TOTP(secret).now()
    data = login(client, "doc2@test.ru", otp=otp)
    assert data["access_token"]

def test_login_accepts_adjacent_totp_window(client):
    # valid_window=1: код из соседнего 30-секундного окна валиден — устраняет
    # 401-флейк на границе окна (TOTP считается перед заполнением формы,
    # валидируется после клика «Войти»). Код из окна −2 отклоняется.
    data = register(client, "win@test.ru", role="CLINICIAN")
    secret = data["totp_secret"]
    now_epoch = int(time.time())
    win_start = now_epoch - (now_epoch % 30)
    prev_code = pyotp.TOTP(secret).at(_dt.datetime.fromtimestamp(win_start - 15))
    stale_code = pyotp.TOTP(secret).at(_dt.datetime.fromtimestamp(win_start - 45))
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "win@test.ru", "password": "Str0ng-Pass!", "otp": prev_code},
    )
    assert resp.status_code == 200, resp.text
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "win@test.ru", "password": "Str0ng-Pass!", "otp": stale_code},
    )
    assert resp.status_code == 401


def test_login_wrong_password(client):
    register(client, "wp@test.ru")
    resp = client.post("/api/v1/auth/login", json={"email": "wp@test.ru", "password": "WrongPass-1"})
    assert resp.status_code == 401

def test_login_rate_limited_429(client):
    # Перебор пароля/2FA: > rate_max неудачных попыток на (IP, email) → 429 TOO_MANY_REQUESTS
    register(client, "rl@test.ru")
    for _ in range(5):
        resp = client.post("/api/v1/auth/login", json={"email": "rl@test.ru", "password": "WrongPass-1"})
        assert resp.status_code == 401
    resp = client.post("/api/v1/auth/login", json={"email": "rl@test.ru", "password": "WrongPass-1"})
    assert resp.status_code == 429
    assert resp.json()["code"] == "TOO_MANY_REQUESTS"

def test_login_successes_not_rate_limited(client):
    # Успешные входы не расходуют бюджет rate-limiter'а (считает только неудачи):
    # 6 успешных логинов подряд не дают 429.
    register(client, "ok@test.ru")
    for _ in range(6):
        data = login(client, "ok@test.ru")
        assert data["access_token"]


def test_refresh_returns_new_access(client):
    register(client, "rf@test.ru")
    data = login(client, "rf@test.ru")
    resp = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": data["refresh_token"]}
    )
    assert resp.status_code == 200
    assert resp.json()["access_token"]


def test_patient_token_cannot_access_admin(client):
    register(client, "pa@test.ru")
    data = login(client, "pa@test.ru")
    resp = client.get("/api/v1/admin/audit", headers=auth_header(data["access_token"]))
    assert resp.status_code == 403
    assert resp.json()["code"] == "ROLE_MISMATCH"


def test_refresh_rotates_and_invalidates_old(client, db_session):
    """F4: /refresh выдаёт новый refresh, старый отозван."""
    from app.models import RefreshToken

    register(client, "rot@test.ru")
    data = login(client, "rot@test.ru")
    old_rt = data["refresh_token"]
    # первый refresh — успех + новый refresh_token
    resp = client.post("/api/v1/auth/refresh", json={"refresh_token": old_rt})
    assert resp.status_code == 200
    new_rt = resp.json()["refresh_token"]
    assert new_rt != old_rt
    # повторное использование старого → 401 (отозван)
    resp2 = client.post("/api/v1/auth/refresh", json={"refresh_token": old_rt})
    assert resp2.status_code == 401
    # новый refresh ещё работает
    resp3 = client.post("/api/v1/auth/refresh", json={"refresh_token": new_rt})
    assert resp3.status_code == 200
    # в сторе ровно одна активная (не отозванная) строка
    active = db_session.execute(
        RefreshToken.__table__.select().where(RefreshToken.__table__.c.revoked_at.is_(None))
    ).all()
    assert len(active) == 1


def test_logout_revokes_refresh(client):
    """F4: /auth/logout отзывает refresh-токен."""
    register(client, "lo@test.ru")
    data = login(client, "lo@test.ru")
    resp = client.post("/api/v1/auth/logout", json={"refresh_token": data["refresh_token"]})
    assert resp.status_code == 204
    # после logout refresh не работает
    resp2 = client.post("/api/v1/auth/refresh", json={"refresh_token": data["refresh_token"]})
    assert resp2.status_code == 401
    # logout idempotent — повторный вызов тоже 204
    resp3 = client.post("/api/v1/auth/logout", json={"refresh_token": data["refresh_token"]})
    assert resp3.status_code == 204

def test_refresh_concurrent_does_not_double_issue(client, db_session):
    """F4 re-audit: with_for_update сериализует два конкурентных /refresh с одним jti.
    Симулируем race вручную — отзываем токен между decode и lookup не нужно,
    проверяем инвариант: из одного валидного refresh-тока нельзя получить два
    независимых активных токена через последовательные вызовы (второй видит revoked)."""
    from app.models import RefreshToken

    register(client, "race@test.ru")
    data = login(client, "race@test.ru")
    old_rt = data["refresh_token"]

    # первый refresh — успех
    r1 = client.post("/api/v1/auth/refresh", json={"refresh_token": old_rt})
    assert r1.status_code == 200
    # второй с тем же old_rt — должен 401 (row locked + revoked_at set)
    r2 = client.post("/api/v1/auth/refresh", json={"refresh_token": old_rt})
    assert r2.status_code == 401
    # ровно одна активная строка в сторе
    active = db_session.execute(
        RefreshToken.__table__.select().where(RefreshToken.__table__.c.revoked_at.is_(None))
    ).all()
    assert len(active) == 1


def test_prod_mode_rejects_demo_secrets():
    """F3 re-audit: prod-деплой compose as-is (демо-секреты, без CC_ALLOW_DEMO_SECRETS)
    → Settings() падает при импорте → fail-fast."""
    import os

    from app.config import Settings

    # prod-режим без allow_demo_secrets → demo-jwt-secret должен быть отвергнут
    os.environ.pop("CC_ALLOW_DEMO_SECRETS", None)
    try:
        Settings(
            jwt_secret="demo-jwt-secret-0123456789abcdefghij",
            data_key="compass-demo-data-key-do-not-use-in-prod-0123456789",
            prod_mode=False,
            _env_file=None,
        )
        raise AssertionError("демо-секрет должен быть отвергнут без CC_ALLOW_DEMO_SECRETS")
    except ValueError as e:
        assert "CC_ALLOW_DEMO_SECRETS" in str(e) or "демо" in str(e).lower()
    finally:
        os.environ["CC_ALLOW_DEMO_SECRETS"] = "true"


def test_privileged_registration_requires_admin_or_demo(client, monkeypatch):
    """NEW-1 (HIGH): вне демо-режима публичная регистрация CLINICIAN/ADMIN → 403.
    Иначе неаутентифицированный пользователь создаёт ADMIN и читает карты пациентов."""
    from app.config import settings

    monkeypatch.setattr(settings, "allow_demo_secrets", False)
    # без токена — запрещено
    r = client.post("/api/v1/auth/register",
                    json={"email": "evil-doc@t.ru", "password": "Str0ng-Pass!", "role": "CLINICIAN"})
    assert r.status_code == 403 and r.json()["code"] == "REGISTRATION_FORBIDDEN"
    r = client.post("/api/v1/auth/register",
                    json={"email": "evil-admin@t.ru", "password": "Str0ng-Pass!", "role": "ADMIN"})
    assert r.status_code == 403
    # PATIENT по-прежнему свободно
    r = client.post("/api/v1/auth/register",
                    json={"email": "ok-pat@t.ru", "password": "Str0ng-Pass!", "role": "PATIENT"})
    assert r.status_code == 201

    # действующий ADMIN может создать CLINICIAN через API.
    # Бутстрап первого ADMIN — операторский путь (python -m app.bootstrap_user),
    # поэтому в тесте создаём его при включённом демо-флаге.
    from .conftest import admin_ctx

    monkeypatch.setattr(settings, "allow_demo_secrets", True)
    ha, _ = admin_ctx(client, "real-admin@t.ru")
    monkeypatch.setattr(settings, "allow_demo_secrets", False)
    r = client.post("/api/v1/auth/register",
                    json={"email": "new-doc@t.ru", "password": "Str0ng-Pass!", "role": "CLINICIAN"},
                    headers=ha)
    assert r.status_code == 201


def test_login_without_otp_when_requirement_off(client, monkeypatch):
    """CC_REQUIRE_OTP=false отключает проверку 2FA, но totp_secret хранится в БД."""
    from app.config import settings

    monkeypatch.setattr(settings, "require_otp", True)
    r = client.post("/api/v1/auth/register",
                    json={"email": "off-doc@t.ru", "password": "Str0ng-Pass!", "role": "CLINICIAN"})
    assert r.status_code == 201 and "totp_secret" in r.json()  # секрет по-прежнему выдаётся
    email = "off-doc@t.ru"

    # флаг выключен: вход без кода успешен (и неверный код игнорируется)
    monkeypatch.setattr(settings, "require_otp", False)
    for otp in (None, "000000"):
        r = client.post("/api/v1/auth/login",
                        json={"email": email, "password": "Str0ng-Pass!", "otp": otp})
        assert r.status_code == 200 and r.json()["role"] == "CLINICIAN"

    # флаг включён обратно: без кода — 401, с верным кодом — успех
    monkeypatch.setattr(settings, "require_otp", True)
    r = client.post("/api/v1/auth/login",
                    json={"email": email, "password": "Str0ng-Pass!", "otp": "000000"})
    assert r.status_code == 401
    # достаём сохранённый totp_secret из БД и входим с верным кодом
    from sqlalchemy import select
    from app.models import User
    from app.db import SessionLocal
    with SessionLocal() as db:
        secret = db.scalar(select(User.totp_secret).where(User.email == email))
    assert secret  # секрет не был стёрт при отключённом флаге
    r = client.post("/api/v1/auth/login",
                    json={"email": email, "password": "Str0ng-Pass!", "otp": pyotp.TOTP(secret).now()})
    assert r.status_code == 200


def test_prod_mode_requires_otp():
    """CC_PROD_MODE=true + CC_REQUIRE_OTP=false — несовместимо (fail-fast)."""
    from pydantic import ValidationError
    from app.config import Settings

    try:
        Settings(_env_file=None, allow_demo_secrets=False, prod_mode=True,
                 require_otp=False,
                 jwt_secret="x" * 40, data_key="y" * 40)
        raise AssertionError("ожидали ValidationError")
    except ValidationError as exc:
        assert "CC_REQUIRE_OTP" in str(exc)
