"""FR-48: коды e-mail — сброс пароля и подтверждение входа (docs/03 §15, docs/04 §3.1).

Проверяем контракт, а не реализацию: ответы не выдают наличие аккаунта, коды
одноразовые и с бюджетом попыток, пароль меняется только по верному коду,
старые сессии умирают, вход по коду выдаёт ту же пару токенов.
"""

from __future__ import annotations

from sqlalchemy import text

from app.config import settings

from .conftest import login, register

OLD_PASSWORD = "Str0ng-Pass!"
NEW_PASSWORD = "N3w-Pass-123!"


def _reset_request(client, email):
    return client.post("/api/v1/auth/password/reset-request", json={"email": email})


def _reset(client, email, code, password=NEW_PASSWORD):
    return client.post(
        "/api/v1/auth/password/reset",
        json={"email": email, "code": code, "password": password},
    )


def test_reset_request_unknown_email_is_202_and_sends_nothing(client, engine):
    resp = _reset_request(client, "nobody@test.ru")
    assert resp.status_code == 202
    assert resp.json() == {"sent": True}  # ни demo_code, ни подсказки о наличии
    with engine.begin() as conn:
        assert conn.scalar(text("select count(*) from auth_codes")) == 0


def test_reset_request_resend_cooldown_429(client):
    register(client, "cool@test.ru")
    assert _reset_request(client, "cool@test.ru").status_code == 202
    again = _reset_request(client, "cool@test.ru")
    assert again.status_code == 429
    assert again.json()["code"] == "CODE_COOLDOWN"


def test_password_reset_end_to_end(client, engine):
    user_id = register(client, "reset@test.ru", OLD_PASSWORD)["id"]
    sent = _reset_request(client, "reset@test.ru")
    assert sent.status_code == 202
    code = sent.json()["demo_code"]
    assert len(code) == 6 and code.isdigit()

    resp = _reset(client, "reset@test.ru", code)
    assert resp.status_code == 204, resp.text
    assert resp.content == b""

    assert login(client, "reset@test.ru", NEW_PASSWORD)["access_token"]
    wrong = client.post(
        "/api/v1/auth/login", json={"email": "reset@test.ru", "password": OLD_PASSWORD}
    )
    assert wrong.status_code == 401

    # Аудит: только факты и id — ни e-mail, ни кода.
    with engine.begin() as conn:
        rows = conn.execute(text("select action, object_id from audit_log")).fetchall()
    assert {"PASSWORD_RESET_REQUEST", "PASSWORD_RESET"} <= {row[0] for row in rows}
    assert all(row[1] == user_id for row in rows)


def test_password_reset_wrong_code_422(client):
    register(client, "wrong@test.ru", OLD_PASSWORD)
    _reset_request(client, "wrong@test.ru")
    resp = _reset(client, "wrong@test.ru", "000001")
    assert resp.status_code == 422
    assert resp.json()["code"] == "CODE_INVALID"
    # Пароль не изменился.
    assert login(client, "wrong@test.ru", OLD_PASSWORD)["access_token"]


def test_password_reset_expired_code_422(client, engine):
    register(client, "exp@test.ru", OLD_PASSWORD)
    code = _reset_request(client, "exp@test.ru").json()["demo_code"]
    with engine.begin() as conn:
        conn.execute(text("update auth_codes set expires_at = now() - interval '1 second'"))
    resp = _reset(client, "exp@test.ru", code)
    assert resp.status_code == 422
    assert resp.json()["code"] == "CODE_INVALID"


def test_password_reset_five_attempts_then_invalid(client):
    register(client, "brute@test.ru", OLD_PASSWORD)
    code = _reset_request(client, "brute@test.ru").json()["demo_code"]
    for _ in range(5):
        assert _reset(client, "brute@test.ru", "999999").status_code == 422
    # Бюджет исчерпан: верный код больше не принимается.
    resp = _reset(client, "brute@test.ru", code)
    assert resp.status_code == 422
    assert resp.json()["code"] == "CODE_INVALID"


def test_password_reset_code_is_single_use(client):
    register(client, "reuse@test.ru", OLD_PASSWORD)
    code = _reset_request(client, "reuse@test.ru").json()["demo_code"]
    assert _reset(client, "reuse@test.ru", code).status_code == 204
    again = _reset(client, "reuse@test.ru", code, password="An0ther-Pass!")
    assert again.status_code == 422
    assert again.json()["code"] == "CODE_INVALID"


def test_password_reset_kills_refresh_tokens(client):
    register(client, "sessions@test.ru", OLD_PASSWORD)
    data = login(client, "sessions@test.ru", OLD_PASSWORD)
    code = _reset_request(client, "sessions@test.ru").json()["demo_code"]
    assert _reset(client, "sessions@test.ru", code).status_code == 204
    resp = client.post("/api/v1/auth/refresh", json={"refresh_token": data["refresh_token"]})
    assert resp.status_code == 401


def test_reset_code_stored_hashed(client, engine):
    register(client, "hash@test.ru", OLD_PASSWORD)
    code = _reset_request(client, "hash@test.ru").json()["demo_code"]
    with engine.begin() as conn:
        rows = conn.execute(text("select code_hash from auth_codes")).fetchall()
    assert len(rows) == 1
    assert code not in rows[0][0]
    assert len(rows[0][0]) == 64


def test_email_otp_login_issues_tokens_only_after_code(client, engine, monkeypatch):
    monkeypatch.setattr(settings, "email_otp", True)
    user_id = register(client, "otp@test.ru", OLD_PASSWORD)["id"]

    resp = client.post(
        "/api/v1/auth/login", json={"email": "otp@test.ru", "password": OLD_PASSWORD}
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["otp_required"] == "email"
    assert "access_token" not in body and "refresh_token" not in body
    assert "cc_access" not in resp.cookies

    bad = client.post(
        "/api/v1/auth/login/otp", json={"challenge": body["challenge"], "code": "000001"}
    )
    assert bad.status_code == 422
    assert bad.json()["code"] == "CODE_INVALID"

    ok = client.post(
        "/api/v1/auth/login/otp",
        json={"challenge": body["challenge"], "code": body["demo_code"]},
    )
    assert ok.status_code == 200, ok.text
    data = ok.json()
    assert data["access_token"] and data["refresh_token"] and data["role"] == "PATIENT"
    assert "cc_access" in ok.cookies  # та же cookie-сессия, что у пароля

    with engine.begin() as conn:
        rows = conn.execute(text("select action, object_id from audit_log")).fetchall()
    assert {"LOGIN_OTP_SENT", "LOGIN_OTP_FAILED", "LOGIN_OTP_OK"} <= {row[0] for row in rows}
    assert all(row[1] == user_id for row in rows)  # ни e-mail, ни кода


def test_email_otp_resend_cooldown_429(client, monkeypatch):
    monkeypatch.setattr(settings, "email_otp", True)
    register(client, "otpcool@test.ru", OLD_PASSWORD)
    body = {"email": "otpcool@test.ru", "password": OLD_PASSWORD}
    assert client.post("/api/v1/auth/login", json=body).status_code == 202
    again = client.post("/api/v1/auth/login", json=body)
    assert again.status_code == 429
    assert again.json()["code"] == "CODE_COOLDOWN"


def test_email_otp_login_without_channel_fails_closed(client, monkeypatch):
    monkeypatch.setattr(settings, "email_otp", True)
    monkeypatch.setattr(settings, "prod_mode", True)
    monkeypatch.setattr(settings, "smtp_host", "")
    register(client, "prodlogin@test.ru", OLD_PASSWORD)
    resp = client.post(
        "/api/v1/auth/login", json={"email": "prodlogin@test.ru", "password": OLD_PASSWORD}
    )
    assert resp.status_code == 503
    assert resp.json()["code"] == "MAIL_UNAVAILABLE"
    assert "challenge" not in resp.json()


def test_email_otp_off_keeps_password_login(client):
    register(client, "plain@test.ru", OLD_PASSWORD)
    resp = client.post(
        "/api/v1/auth/login", json={"email": "plain@test.ru", "password": OLD_PASSWORD}
    )
    assert resp.status_code == 200
    assert set(resp.json()) == {"access_token", "refresh_token", "role"}


def test_email_otp_off_never_sends_code(client, engine):
    register(client, "nocode@test.ru", OLD_PASSWORD)
    login(client, "nocode@test.ru", OLD_PASSWORD)
    with engine.begin() as conn:
        assert conn.scalar(text("select count(*) from auth_codes where purpose = 'login'")) == 0


def test_prod_mode_without_smtp_fails_closed(client, engine, monkeypatch):
    register(client, "prod@test.ru", OLD_PASSWORD)
    monkeypatch.setattr(settings, "prod_mode", True)
    monkeypatch.setattr(settings, "smtp_host", "")
    for address in ("prod@test.ru", "ghost@test.ru"):
        resp = _reset_request(client, address)
        # 503 одинаков для существующего и несуществующего адреса:
        # статус не превращается в признак наличия аккаунта.
        assert resp.status_code == 503, (address, resp.text)
        body = resp.json()
        assert body["code"] == "MAIL_UNAVAILABLE"
        assert "demo_code" not in body
    with engine.begin() as conn:
        assert conn.scalar(text("select count(*) from auth_codes")) == 0


def test_smtp_failure_keeps_202_for_existing_and_unknown(client, engine, monkeypatch):
    register(client, "smtpdown@test.ru", OLD_PASSWORD)
    monkeypatch.setattr(settings, "smtp_host", "127.0.0.1")
    monkeypatch.setattr(settings, "smtp_port", 1)  # заведомо закрытый порт
    monkeypatch.setattr(settings, "smtp_from", "noreply@test.ru")
    monkeypatch.setattr(settings, "smtp_tls", False)
    for address in ("smtpdown@test.ru", "ghost-smtp@test.ru"):
        # Сбой доставки не меняет ответ: 202 и никакого demo_code — иначе
        # 503/тело выдавали бы существующие адреса при упавшем релее.
        resp = _reset_request(client, address)
        assert resp.status_code == 202, (address, resp.text)
        assert resp.json() == {"sent": True}
    with engine.begin() as conn:
        # Код существующего адреса сохранён (доставка уже после ответа),
        # а сбой виден только в аудите — без e-mail и кода.
        assert conn.scalar(text("select count(*) from auth_codes")) == 1
        assert conn.scalar(text("select count(*) from audit_log "
                               "where action = 'PASSWORD_RESET_DELIVERY_FAILED'")) == 1
        assert conn.scalar(text("select count(*) from audit_log "
                               "where action = 'PASSWORD_RESET_REQUEST'")) == 1
