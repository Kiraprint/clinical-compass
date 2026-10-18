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