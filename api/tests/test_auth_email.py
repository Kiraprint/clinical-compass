"""FR-48: коды e-mail — сброс пароля и подтверждение входа (docs/03 §15, docs/04 §3.1).

Проверяем контракт, а не реализацию: ответы не выдают наличие аккаунта, коды
одноразовые и с бюджетом попыток, пароль меняется только по верному коду,
старые сессии умирают, вход по коду выдаёт ту же пару токенов.
"""

from __future__ import annotations

from sqlalchemy import text

from app.config import settings
from app.services import mailer

from .conftest import auth_header, login, register

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

def test_demo_code_needs_explicit_flag(client, engine, monkeypatch):
    """CC_ALLOW_DEMO_SECRETS сам по себе код не отдаёт.

    Публичный демо-контур запускается с allow_demo_secrets (иначе api не
    стартует на демо-JWT), поэтому одного этого флага мало: без явного
    CC_MAIL_DEMO_CODES запрос сброса не должен возвращать код чужого аккаунта.
    """
    register(client, "gate@test.ru", OLD_PASSWORD)
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "smtp_from", "")
    monkeypatch.setattr(settings, "mail_demo_codes", False)
    for address in ("gate@test.ru", "ghost-gate@test.ru"):
        resp = _reset_request(client, address)
        # 503 одинаков для существующего и несуществующего адреса.
        assert resp.status_code == 503, (address, resp.text)
        body = resp.json()
        assert body["code"] == "MAIL_UNAVAILABLE"
        assert "demo_code" not in body

    # Второй шаг входа по e-mail тоже закрыт тем же отказом, а не 500.
    monkeypatch.setattr(settings, "email_otp", True)
    resp = client.post("/api/v1/auth/login",
                       json={"email": "gate@test.ru", "password": OLD_PASSWORD})
    assert resp.status_code == 503, resp.text
    assert resp.json()["code"] == "MAIL_UNAVAILABLE"

    with engine.begin() as conn:
        # Ни одного кода в БД: отказ случился до выпуска.
        assert conn.scalar(text("select count(*) from auth_codes")) == 0

    # С явным флагом демо-стенд работает как раньше.
    monkeypatch.setattr(settings, "mail_demo_codes", True)
    monkeypatch.setattr(settings, "email_otp", False)
    resp = _reset_request(client, "gate@test.ru")
    assert resp.status_code == 202, resp.text
    assert len(resp.json()["demo_code"]) == 6


# --- FR-48 (v2): подтверждение адреса (мягкое, баннер) -----------------------

def _me(client, token):
    return client.get("/api/v1/auth/me", headers=auth_header(token))


def _verify_request(client, token):
    return client.post("/api/v1/auth/email/verify/request", headers=auth_header(token))


def _verify(client, token, code):
    return client.post("/api/v1/auth/email/verify", json={"code": code},
                       headers=auth_header(token))


def test_me_reports_unverified_address_and_requires_token(client):
    assert client.get("/api/v1/auth/me").status_code == 401
    register(client, "verify-me@test.ru", OLD_PASSWORD)
    token = login(client, "verify-me@test.ru", OLD_PASSWORD)["access_token"]
    body = _me(client, token).json()
    assert body["email"] == "verify-me@test.ru"
    assert body["email_verified"] is False


def test_register_without_channel_issues_no_code(client, engine):
    """Без SMTP регистрация не плодит коды, которые некому отправить."""
    register(client, "verify-off@test.ru", OLD_PASSWORD)
    assert client.get("/api/v1/auth/config").json()["mail"] is False
    with engine.connect() as conn:
        assert conn.scalar(text("select count(*) from auth_codes")) == 0


def test_register_sends_verification_email(client, engine, monkeypatch):
    sent: list[tuple[str, str, str]] = []

    async def fake_send(to: str, subject: str, code: str) -> None:
        sent.append((to, subject, code))

    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_from", "noreply@test.ru")
    monkeypatch.setattr("app.services.mailer.send_code", fake_send)

    body = register(client, "verify-on@test.ru", OLD_PASSWORD)
    assert body["email_verified"] is False
    assert client.get("/api/v1/auth/config").json()["mail"] is True
    # BackgroundTasks в TestClient выполняются до возврата из запроса.
    assert [(to, subject) for to, subject, _ in sent] == [
        ("verify-on@test.ru", "Подтверждение адреса")
    ]
    with engine.connect() as conn:
        rows = conn.execute(text(
            "select purpose, code_hash from auth_codes where purpose = 'verify'"
        )).all()
    assert len(rows) == 1
    # В БД не код, а хеш: сравниваем с sha256(code + CC_DATA_KEY).
    assert rows[0][1] != sent[0][2]
    assert len(rows[0][1]) == 64


def test_email_verify_confirms_address(client, monkeypatch):
    register(client, "verify-ok@test.ru", OLD_PASSWORD)
    token = login(client, "verify-ok@test.ru", OLD_PASSWORD)["access_token"]
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "smtp_from", "")
    monkeypatch.setattr(settings, "mail_demo_codes", True)

    requested = _verify_request(client, token)
    assert requested.status_code == 202, requested.text
    code = requested.json()["demo_code"]

    assert _verify(client, token, "000000" if code != "000000" else "111111").status_code == 422
    assert _verify(client, token, code).json() == {"email_verified": True}
    assert _me(client, token).json()["email_verified"] is True
    # Повтор — 409: адрес уже подтверждён, код второй раз не нужен.
    assert _verify(client, token, code).status_code == 409
    assert _verify_request(client, token).status_code == 409


def test_email_verify_request_cooldown(client, monkeypatch):
    register(client, "verify-cool@test.ru", OLD_PASSWORD)
    token = login(client, "verify-cool@test.ru", OLD_PASSWORD)["access_token"]
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "smtp_from", "")
    monkeypatch.setattr(settings, "mail_demo_codes", True)
    assert _verify_request(client, token).status_code == 202
    second = _verify_request(client, token)
    assert second.status_code == 429
    assert second.json()["code"] == "CODE_COOLDOWN"


def test_email_verify_code_single_use_and_unknown_code_422(client, engine, monkeypatch):
    register(client, "verify-once@test.ru", OLD_PASSWORD)
    token = login(client, "verify-once@test.ru", OLD_PASSWORD)["access_token"]
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "smtp_from", "")
    monkeypatch.setattr(settings, "mail_demo_codes", True)
    code = _verify_request(client, token).json()["demo_code"]
    assert _verify(client, token, code).status_code == 200
    # Отмечаем адрес неподтверждённым обратно: старый код уже израсходован.
    with engine.begin() as conn:
        conn.execute(text("update users set email_verified_at = null "
                          "where email = 'verify-once@test.ru'"))
    reused = _verify(client, token, code)
    assert reused.status_code == 422
    assert reused.json()["code"] == "CODE_INVALID"


def test_demo_code_needs_flag_for_verification_too(client, engine, monkeypatch):
    """Демо-код подтверждения не выдаётся без явного CC_MAIL_DEMO_CODES."""
    register(client, "verify-nodemo@test.ru", OLD_PASSWORD)
    token = login(client, "verify-nodemo@test.ru", OLD_PASSWORD)["access_token"]
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "smtp_from", "")
    monkeypatch.setattr(settings, "mail_demo_codes", False)
    for _ in range(2):
        resp = _verify_request(client, token)
        assert resp.status_code == 202
        assert resp.json() == {"sent": False}
    with engine.connect() as conn:
        assert conn.scalar(text(
            "select count(*) from auth_codes where purpose = 'verify'"
        )) == 0


def test_verify_request_reports_smtp_failure_and_does_not_burn_cooldown(
    client, engine, monkeypatch
):
    """`sent:true` обязан означать «письмо ушло»: отказ SMTP — `sent:false`."""
    register(client, "verify-fail@test.ru", OLD_PASSWORD)
    token = login(client, "verify-fail@test.ru", OLD_PASSWORD)["access_token"]
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_from", "noreply@test.ru")

    async def boom(to: str, subject: str, code: str) -> None:
        raise mailer.MailUnavailable("down")

    monkeypatch.setattr("app.services.mailer.send_code", boom)
    first = _verify_request(client, token)
    assert first.status_code == 202
    assert first.json() == {"sent": False}
    # Пауза не съедена: письма не было.
    assert _verify_request(client, token).status_code == 202
    with engine.connect() as conn:
        assert conn.scalar(text(
            "select count(*) from audit_log where action = 'EMAIL_VERIFY_DELIVERY_FAILED'"
        )) == 2
        # Недоставленный код не коммитится: ретрай при упавшем канале не копит
        # мусор в auth_codes (иначе строка живёт до самого TTL).
        assert conn.scalar(text(
            "select count(*) from auth_codes where purpose = 'verify'"
        )) == 0


def test_resend_invalidates_previous_verify_code(client, monkeypatch):
    """Повторное письмо гасит прошлый код: иначе в ящике два «рабочих» кода."""
    sent: list[str] = []

    async def fake_send(to: str, subject: str, code: str) -> None:
        sent.append(code)

    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_from", "noreply@test.ru")
    monkeypatch.setattr("app.services.mailer.send_code", fake_send)
    register(client, "verify-resend@test.ru", OLD_PASSWORD)
    token = login(client, "verify-resend@test.ru", OLD_PASSWORD)["access_token"]
    assert _verify_request(client, token).json() == {"sent": True}
    assert len(sent) == 2 and sent[0] != sent[1]

    assert _verify(client, token, sent[0]).status_code == 422
    assert _verify(client, token, sent[1]).json() == {"email_verified": True}
