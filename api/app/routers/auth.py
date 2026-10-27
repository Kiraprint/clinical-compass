# E3: auth. Контракт: docs/04 §3.1; тесты — docs/06 §3.
# FR-48 (E17): коды e-mail — сброс пароля и подтверждение входа (docs/03 §15).
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
import uuid

from datetime import datetime, timedelta, timezone
import anyio
from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db as db_module
from app import security
from app.config import settings
from app.db import get_db
from app.errors import Problem
from app.models import AuthCode, RefreshToken, User
from app.ratelimit import acheck_rate, arecord_rate_hit
from app.services import audit, mailer
from app.deps import get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])

ALLOWED_ROLES = ("PATIENT", "CLINICIAN", "ADMIN")

# FR-48: TTL, бюджет попыток и пауза повторной отправки — общий контракт
# сброса пароля и входа по коду (docs/03 §15, docs/04 §3.1).
CODE_TTL = timedelta(minutes=10)

CODE_MAX_ATTEMPTS = 5
CODE_RESEND_SECONDS = 60.0

logger = logging.getLogger(__name__)

# Пауза в памяти процесса (как у rate limiter): одинаковое поведение для
# существующего и несуществующего адреса — перебором нельзя узнать, есть ли
# аккаунт. Потолок словаря не даёт расти памяти при переборе адресов.
_cooldowns: dict[str, float] = {}
_cooldowns_lock = threading.Lock()
_COOLDOWNS_MAX = 4096

class RegisterBody(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    role: str = Field(pattern="^(PATIENT|CLINICIAN|ADMIN)$")

class LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str
    otp: str | None = None

class RefreshBody(BaseModel):
    refresh_token: str | None = None  # F9: опционально — может прийти из cookie

class EmailBody(BaseModel):
    email: str = Field(min_length=3, max_length=255)

class PasswordResetBody(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    code: str = Field(min_length=4, max_length=12)
    password: str = Field(min_length=8, max_length=128)

class LoginOtpBody(BaseModel):
    challenge: str = Field(min_length=8, max_length=64)
    code: str = Field(min_length=4, max_length=12)

@router.get("/config")
def auth_config():
    """Публичная конфигурация входа (без токена): CC_REQUIRE_OTP и канал почты.

    `mail` нужен интерфейсу, чтобы не показывать «подтвердите адрес» там, где
    письма физически не отправляются (демо-стенд без SMTP, e2e).
    """
    return {"require_otp": settings.require_otp, "mail": mailer.configured()}

def _set_auth_cookies(response: Response, access: str, refresh: str) -> None:
    """F9: httpOnly cookies для access+refresh токенов.
    SameSite=Lax — CSRF-защита (cross-site POST блокируется, top-level GET разрешён).
    Secure — только HTTPS (configurable через CC_COOKIE_SECURE для HTTP-демо)."""
    secure = settings.cookie_secure
    response.set_cookie("cc_access", access, httponly=True, samesite="lax",
                        secure=secure, path="/api/v1", max_age=settings.access_ttl_minutes * 60)
    response.set_cookie("cc_refresh", refresh, httponly=True, samesite="lax",
                        secure=secure, path="/api/v1/auth", max_age=settings.refresh_ttl_days * 86400)

def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie("cc_access", path="/api/v1")
    response.delete_cookie("cc_refresh", path="/api/v1/auth")

async def _privileged_registration_allowed(request: Request, db: AsyncSession) -> bool:
    """NEW-1: привилегированные роли — только демо-режим или действующий ADMIN."""
    if settings.allow_demo_secrets:
        return True
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return False
    try:
        payload = security.decode_token(auth.removeprefix("Bearer "))
    except Exception:
        return False
    if payload.get("type") != "access":
        return False
    actor = await db.get(User, uuid.UUID(payload["sub"]))
    return actor is not None and actor.role == "ADMIN"

def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"

def _cooldown_ok(key: str) -> bool:
    """FR-48: True раз в CODE_RESEND_SECONDS на ключ; False — 429 CODE_COOLDOWN."""
    now = time.monotonic()
    with _cooldowns_lock:
        if len(_cooldowns) > _COOLDOWNS_MAX:
            _cooldowns.clear()
        last = _cooldowns.get(key)
        if last is not None and now - last < CODE_RESEND_SECONDS:
            return False
        _cooldowns[key] = now
        return True

def _cooldown_clear(key: str) -> None:
    """Неотправленный код не должен съедать паузу: письма ведь не было."""
    with _cooldowns_lock:
        _cooldowns.pop(key, None)

def _demo_codes_allowed() -> bool:
    """Демо-код в ответе — только по явному CC_MAIL_DEMO_CODES и вне прод-режима."""
    return settings.mail_demo_codes and not settings.prod_mode

def _ensure_channel() -> None:
    """FR-48: канал проверяется ДО выборки пользователя.

    Прод без SMTP обязан ответить 503 всем адресам одинаково: иначе 503
    превращается в признак «такой аккаунт есть» (202 у несуществующего и 503
    у существующего). Демо-код отдаётся только при явном `CC_MAIL_DEMO_CODES`
    (стенды и e2e): сам по себе `CC_ALLOW_DEMO_SECRETS` публичный контур к
    сбросу чужого пароля не открывает.
    """
    if mailer.configured():
        return
    mailer.warn_unconfigured()
    if not _demo_codes_allowed():
        raise Problem(503, "MAIL_UNAVAILABLE", "Канал e-mail не настроен: коды подтверждения недоступны")

def _new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"

def _code_hash(code: str) -> str:
    """SHA-256(code + CC_DATA_KEY) — как в rate limiter: в БД кода нет."""
    return hashlib.sha256(f"{code}:{settings.data_key}".encode()).hexdigest()

async def _issue_code(db: AsyncSession, user_id: uuid.UUID, purpose: str) -> tuple[str, AuthCode]:
    """Создать код в текущей транзакции (flush → известен id-challenge)."""
    code = _new_code()
    row = AuthCode(
        user_id=user_id,
        purpose=purpose,
        code_hash=_code_hash(code),
        expires_at=datetime.now(timezone.utc) + CODE_TTL,
    )
    db.add(row)
    await db.flush()
    return code, row


async def _retire_codes(db: AsyncSession, user_id: uuid.UUID, purpose: str) -> None:
    """Погасить живые коды того же назначения перед выпуском нового.

    Повторная отправка обесценивает предыдущее письмо: иначе в ящике лежат два
    кода, а проверяется только последний, и «старый» выглядит рабочим.
    """
    rows = await db.scalars(
        select(AuthCode).where(
            AuthCode.user_id == user_id,
            AuthCode.purpose == purpose,
            AuthCode.consumed_at.is_(None),
        )
    )
    now = datetime.now(timezone.utc)
    for row in rows:
        row.consumed_at = now

async def _latest_code(db: AsyncSession, user_id: uuid.UUID, purpose: str) -> AuthCode | None:
    return await db.scalar(
        select(AuthCode)
        .where(
            AuthCode.user_id == user_id,
            AuthCode.purpose == purpose,
            AuthCode.consumed_at.is_(None),
        )
        .order_by(AuthCode.created_at.desc())
        .limit(1)
        .with_for_update()
    )

def _verify_code_row(row: AuthCode, code: str) -> bool:
    """Одноразовая проверка: TTL → бюджет попыток → хеш. Причина наружу не идёт."""
    now = datetime.now(timezone.utc)
    if row.consumed_at is not None or row.expires_at <= now:
        return False
    if row.attempts >= CODE_MAX_ATTEMPTS:
        return False
    row.attempts += 1
    if not hmac.compare_digest(row.code_hash, _code_hash(code)):
        return False
    row.consumed_at = now
    return True

async def _deliver_code(email: str, subject: str, code: str) -> dict:
    """Отправить код письмом; без SMTP — `demo_code` (решение о режиме — в
    `_ensure_channel`, вызывается до выборки пользователя)."""
    if not mailer.configured():
        return {"demo_code": code} if _demo_codes_allowed() else {}
    try:
        await mailer.send_code(email, subject, code)
    except mailer.MailUnavailable:
        raise Problem(503, "MAIL_UNAVAILABLE", "Не удалось отправить письмо, повторите позже")
    return {}

@router.post("/register", status_code=201)
async def register(
    body: RegisterBody,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Регистрация пользователя.

    NEW-1 (HIGH): публичная регистрация привилегированных ролей (CLINICIAN/ADMIN)
    запрещена вне демо-режима — иначе любой неаутентифицированный пользователь
    создаёт себе ADMIN, выдаёт ростер и читает расшифрованные карты пациентов.
    Демо-стенд разрешает это явно (`CC_ALLOW_DEMO_SECRETS=true`); прод требует
    создания привилегированных ролей аутентифицированным ADMIN.
    """
    email = body.email.strip().lower()
    exists = await db.scalar(select(User).where(User.email == email))
    if exists:
        raise Problem(409, "CONFLICT", "Пользователь с таким email уже зарегистрирован")
    if body.role != "PATIENT" and not await _privileged_registration_allowed(request, db):
        raise Problem(
            403, "REGISTRATION_FORBIDDEN",
            "Создание роли CLINICIAN/ADMIN требует аутентифицированного администратора",
        )
    # argon2id — десятки миллисекунд CPU и 64 МиБ памяти: на event loop это
    # стопор для всех запросов воркера, поэтому хешируем в потоке.
    password_hash = await anyio.to_thread.run_sync(security.hash_password, body.password)
    user = User(email=email, password_hash=password_hash, role=body.role)
    totp_secret: str | None = None
    if body.role != "PATIENT":
        totp_secret = security.generate_totp_secret()
        user.totp_secret = totp_secret
    db.add(user)
    await db.flush()  # нужен id для кода подтверждения адреса
    # FR-48 (v2): подтверждение адреса — мягкое. Код выпускаем только когда
    # канал настроен; без SMTP не плодим коды, которые некому отправить.
    verify_code: str | None = None
    if mailer.configured():
        verify_code, _ = await _issue_code(db, user.id, "verify")
    await db.commit()
    resp: dict = {
        "id": str(user.id),
        "email": user.email,
        "role": user.role,
        "email_verified": False,
    }
    if totp_secret:
        resp["totp_secret"] = totp_secret
    if verify_code:
        # Письмо — после ответа: сбой SMTP не отменяет регистрацию (мягкая
        # проверка), а ожидание чужого сервера не держит запрос.
        background.add_task(_send_verify_code, user.id, email, verify_code, _client(request))
    return resp

@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    email = body.email.strip().lower()
    client = _client(request)
    allowed, retry_after = await acheck_rate((client, email))
    if not allowed:
        raise Problem(429, "TOO_MANY_REQUESTS",
                      f"Слишком много попыток входа, повторите через {retry_after} с")
    user = await db.scalar(select(User).where(User.email == email))
    password_ok = user is not None and await anyio.to_thread.run_sync(
        security.verify_password, body.password, user.password_hash
    )
    if not password_ok:
        await arecord_rate_hit((client, email))
        raise Problem(401, "UNAUTHORIZED", "Неверный email или пароль")
    # FR-48: подтверждение входа пациента кодом на e-mail (CC_EMAIL_OTP).
    # Пароль верный, но токены выдаются только после /auth/login/otp.
    if user.role == "PATIENT" and settings.email_otp:
        _ensure_channel()  # 503 одинаково для всех, до записи кода
        key = f"login:{user.id}"
        if not _cooldown_ok(key):
            raise Problem(429, "CODE_COOLDOWN", "Код уже отправлен, повторно — через 60 с")
        code, row = await _issue_code(db, user.id, "login")
        await audit.record(db, user.id, "LOGIN_OTP_SENT", "user", str(user.id), client)
        try:
            extra = await _deliver_code(user.email, "Код подтверждения входа", code)
        except Problem:
            # Письмо не ушло → не коммитим код и не съедаем паузу.
            await db.rollback()
            _cooldown_clear(key)
            raise
        await db.commit()
        response.status_code = 202
        return {"otp_required": "email", "challenge": str(row.id), **extra}
    if user.role != "PATIENT" and settings.require_otp:
        if not user.totp_secret or not body.otp or not security.verify_totp(user.totp_secret, body.otp):
            await arecord_rate_hit((client, email))
            raise Problem(401, "UNAUTHORIZED", "Требуется корректный код 2FA")
    jti = await _issue_jti(db, user.id)
    await db.commit()
    access = security.create_access_token(user.id, user.role)
    refresh = security.create_refresh_token(user.id, jti)
    _set_auth_cookies(response, access, refresh)  # F9: httpOnly cookies
    return {"access_token": access, "refresh_token": refresh, "role": user.role}

@router.post("/login/otp")
async def login_otp(body: LoginOtpBody, request: Request, response: Response,
                    db: AsyncSession = Depends(get_db)):
    """FR-48: второй шаг входа пациента — код из письма → те же токены, что у пароля."""
    client = _client(request)
    try:
        challenge = uuid.UUID(body.challenge)
    except (ValueError, TypeError):
        raise Problem(422, "CODE_INVALID", "Код недействителен или истёк")
    row = await db.scalar(
        select(AuthCode).where(AuthCode.id == challenge, AuthCode.purpose == "login").with_for_update()
    )
    user = await db.get(User, row.user_id) if row is not None else None
    if user is None or not _verify_code_row(row, body.code):
        if user is not None:
            await arecord_rate_hit((client, user.email))
        await audit.record(db, user.id if user else None, "LOGIN_OTP_FAILED", "user",
                     str(user.id) if user else None, client)
        await db.commit()
        raise Problem(422, "CODE_INVALID", "Код недействителен или истёк")
    await audit.record(db, user.id, "LOGIN_OTP_OK", "user", str(user.id), client)
    jti = await _issue_jti(db, user.id)
    await db.commit()
    access = security.create_access_token(user.id, user.role)
    refresh = security.create_refresh_token(user.id, jti)
    _set_auth_cookies(response, access, refresh)
    return {"access_token": access, "refresh_token": refresh, "role": user.role}

async def _send_reset_code(user_id: uuid.UUID, email: str, code: str, client: str | None) -> None:
    """Доставка письма сброса ПОСЛЕ ответа: сбой не меняет статус и тайминг."""
    try:
        await mailer.send_code(email, "Сброс пароля", code)
    except mailer.MailUnavailable:
        logger.warning("FR-48: письмо сброса пароля не отправлено (SMTP недоступен)")
        if db_module.AsyncSessionLocal is None:  # БД ещё не инициализирована
            return
        async with db_module.AsyncSessionLocal() as session:
            await audit.record(session, None, "PASSWORD_RESET_DELIVERY_FAILED",
                               "user", str(user_id), client)
            await session.commit()


@router.post("/password/reset-request", status_code=202)
async def password_reset_request(body: EmailBody, request: Request,
                                 background: BackgroundTasks,
                                 db: AsyncSession = Depends(get_db)):
    """FR-48: письмо с кодом сброса. Ответ 202 `{sent:true}` — для любого адреса
    и при любом исходе доставки: ни наличие аккаунта, ни состояние почты наружу
    не выдаются. 503 MAIL_UNAVAILABLE — только ненастроенный канал, и проверка
    идёт до выборки пользователя (одинакова для всех). Письмо уходит после
    ответа, поэтому SMTP не влияет на латентность; сбой доставки виден лишь в
    логах и аудите `PASSWORD_RESET_DELIVERY_FAILED`."""
    email = body.email.strip().lower()
    client = _client(request)
    allowed, retry_after = await acheck_rate((client, email))
    if not allowed:
        raise Problem(429, "TOO_MANY_REQUESTS",
                      f"Слишком много запросов, повторите через {retry_after} с")
    await arecord_rate_hit((client, email))  # каждый запрос расходует бюджет
    _ensure_channel()
    key = f"reset:{client}:{email}"
    if not _cooldown_ok(key):
        raise Problem(429, "CODE_COOLDOWN", "Код уже отправлен, повторно — через 60 с")
    user = await db.scalar(select(User).where(User.email == email))
    if user is None:
        return {"sent": True}
    code, _ = await _issue_code(db, user.id, "reset")
    await audit.record(db, None, "PASSWORD_RESET_REQUEST", "user", str(user.id), client)
    await db.commit()
    if not mailer.configured():
        # SMTP нет: код отдаём только по явному CC_MAIL_DEMO_CODES (стенды/e2e)
        return {"sent": True, "demo_code": code} if _demo_codes_allowed() else {"sent": True}
    background.add_task(_send_reset_code, user.id, email, code, client)
    return {"sent": True}

@router.post("/password/reset", status_code=204)
async def password_reset(body: PasswordResetBody, request: Request,
                         db: AsyncSession = Depends(get_db)):
    """FR-48: код + новый пароль. Успех убивает все сессии (refresh-токены)."""
    email = body.email.strip().lower()
    user = await db.scalar(select(User).where(User.email == email))
    row = await _latest_code(db, user.id, "reset") if user is not None else None
    if row is None or not _verify_code_row(row, body.code):
        await db.commit()  # неудачная попытка тоже расходует бюджет
        raise Problem(422, "CODE_INVALID", "Код недействителен или истёк")
    user.password_hash = await anyio.to_thread.run_sync(security.hash_password, body.password)
    await db.execute(delete(RefreshToken).where(RefreshToken.user_id == user.id))
    # Прочие невыкупленные коды сброса больше не нужны — пароль уже сменён.
    await db.execute(delete(AuthCode).where(
        AuthCode.user_id == user.id, AuthCode.purpose == "reset"
    ))
    await audit.record(db, user.id, "PASSWORD_RESET", "user", str(user.id), _client(request))
    await db.commit()
    return Response(status_code=204)

async def _send_verify_code(user_id: uuid.UUID, email: str, code: str, client: str | None) -> None:
    """Подтверждение адреса: письмо после ответа, мягкая семантика."""
    try:
        await mailer.send_code(email, "Подтверждение адреса", code)
    except mailer.MailUnavailable:
        logger.warning("FR-48: письмо подтверждения адреса не отправлено (SMTP недоступен)")
        if db_module.AsyncSessionLocal is None:
            return
        async with db_module.AsyncSessionLocal() as session:
            await audit.record(session, None, "EMAIL_VERIFY_DELIVERY_FAILED",
                               "user", str(user_id), client)
            await session.commit()


class VerifyEmailBody(BaseModel):
    code: str = Field(min_length=4, max_length=12)


@router.get("/me")
async def me(user: User = Depends(get_current_user)):
    """Кто я: роль, адрес и признак подтверждения адреса (для баннера)."""
    return {
        "id": str(user.id),
        "email": user.email,
        "role": user.role,
        "email_verified": user.email_verified_at is not None,
    }


@router.post("/email/verify/request", status_code=202)
async def email_verify_request(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Повторное письмо с кодом подтверждения адреса.

    409 — адрес уже подтверждён, 429 — пауза 60 с между письмами. Код
    подтверждения не участвует в аутентификации, поэтому демо-режим отдаёт
    `demo_code` так же, как сброс пароля (стенды без SMTP). Отправку ждём:
    `{sent:true}` означает, что письмо действительно ушло, а отказ SMTP —
    `{sent:false}` без съеденной паузы.
    """
    if user.email_verified_at is not None:
        raise Problem(409, "EMAIL_ALREADY_VERIFIED", "Адрес уже подтверждён")
    key = f"verify:{user.id}"
    if not _cooldown_ok(key):
        raise Problem(429, "CODE_COOLDOWN", "Письмо уже отправлено: подождите минуту")
    if not mailer.configured() and not _demo_codes_allowed():
        # Отправлять некуда и демо-режим не разрешён: код не выпускаем вовсе
        # (иначе в auth_codes копятся коды, которые никто не увидит) и паузу
        # не съедаем — письма ведь не было.
        _cooldown_clear(key)
        return {"sent": False}
    await _retire_codes(db, user.id, "verify")
    code, _ = await _issue_code(db, user.id, "verify")
    await db.commit()
    if not mailer.configured():
        return {"sent": True, "demo_code": code}
    # Здесь, в отличие от сброса пароля, отправку ждём: `{sent:true}` обязан
    # означать «письмо ушло». Иначе при отказе SMTP интерфейс врёт, а пауза
    # 60 с съедена. Сброс пароля ждать не может — он не должен выдавать
    # наличие аккаунта ни по статусу, ни по времени ответа.
    try:
        await mailer.send_code(user.email, "Подтверждение адреса", code)
    except mailer.MailUnavailable:
        logger.warning("FR-48: письмо подтверждения адреса не отправлено (SMTP отказал)")
        await audit.record(db, user.id, "EMAIL_VERIFY_DELIVERY_FAILED",
                           "user", str(user.id), _client(request))
        await db.commit()
        _cooldown_clear(key)
        return {"sent": False}
    return {"sent": True}


@router.post("/email/verify")
async def email_verify(
    body: VerifyEmailBody,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Подтвердить адрес кодом из письма. Код одноразовый, TTL 10 минут."""
    if user.email_verified_at is not None:
        raise Problem(409, "EMAIL_ALREADY_VERIFIED", "Адрес уже подтверждён")
    row = await _latest_code(db, user.id, "verify")
    if row is None or not _verify_code_row(row, body.code.strip()):
        await audit.record(db, user.id, "EMAIL_VERIFY_FAILED", "user", str(user.id), _client(request))
        await db.commit()
        raise Problem(422, "CODE_INVALID", "Код недействителен или истёк")
    user.email_verified_at = datetime.now(timezone.utc)
    await audit.record(db, user.id, "EMAIL_VERIFIED", "user", str(user.id), _client(request))
    await db.commit()
    return {"email_verified": True}

def _refresh_delta():
    return timedelta(days=settings.refresh_ttl_days)

async def _issue_jti(db: AsyncSession, user_id) -> str:
    """Создать строку RefreshToken и вернуть jti для JWT."""
    jti = uuid.uuid4().hex
    db.add(RefreshToken(jti=jti, user_id=user_id,
                        expires_at=datetime.now(timezone.utc) + _refresh_delta()))
    await db.flush()
    return jti

@router.post("/refresh")
async def refresh(body: RefreshBody | None = None, request: Request = None, response: Response = None, db: AsyncSession = Depends(get_db)):
    """F4: ротация — проверяем jti в сторе, отзываем старый, выдаём новый.
    F9: refresh_token принимается из body ИЛИ из cc_refresh cookie."""
    rt_token = (body.refresh_token if body else None) or (request.cookies.get("cc_refresh", "") if request else "")
    if not rt_token:
        raise Problem(401, "TOKEN_EXPIRED", "Refresh-токен не предоставлен")
    try:
        payload = security.decode_token(rt_token)
    except Exception:
        raise Problem(401, "TOKEN_EXPIRED", "Refresh-токен недействителен или истёк")
    if payload.get("type") != "refresh":
        raise Problem(401, "UNAUTHORIZED", "Ожидается refresh-токен")
    jti = payload.get("jti")
    if not jti:
        raise Problem(401, "UNAUTHORIZED", "Refresh-токен без jti")
    # F4: with_for_update сериализует конкурентные /refresh с одинаковым jti —
    # вторая транзакция ждёт, видит revoked_at и получает 401 (no double-issue).
    rt = await db.scalar(
        select(RefreshToken).where(RefreshToken.jti == jti).with_for_update()
    )
    if rt is None:
        raise Problem(401, "TOKEN_EXPIRED", "Refresh-токен не найден в сторе")
    if rt.revoked_at is not None:
        raise Problem(401, "TOKEN_EXPIRED", "Refresh-токен отозван")
    if rt.expires_at < datetime.now(timezone.utc):
        raise Problem(401, "TOKEN_EXPIRED", "Refresh-токен истёк")
    user = await db.get(User, rt.user_id)
    if user is None:
        raise Problem(401, "UNAUTHORIZED", "Пользователь не найден")
    # ротация: отозвать старый, создать новый
    rt.revoked_at = datetime.now(timezone.utc)
    new_jti = await _issue_jti(db, user.id)
    await db.commit()
    access = security.create_access_token(user.id, user.role)
    new_refresh = security.create_refresh_token(user.id, new_jti)
    _set_auth_cookies(response, access, new_refresh)  # F9: rotate cookies
    return {"access_token": access, "refresh_token": new_refresh}

@router.post("/logout", status_code=204)
async def logout(body: RefreshBody | None = None, request: Request = None, response: Response = None, db: AsyncSession = Depends(get_db)):
    """F4: отзыв refresh-токена (явный выход). Idempotent.
    F9: очищает httpOnly cookies; refresh_token из body или cookie."""
    rt_token = (body.refresh_token if body else None) or (request.cookies.get("cc_refresh", "") if request else "")
    if response:
        _clear_auth_cookies(response)  # F9: всегда очищаем cookies
    if not rt_token:
        return
    try:
        payload = security.decode_token(rt_token)
    except Exception:
        return  # токен уже невалиден — выход idempotent
    jti = payload.get("jti")
    if jti:
        rt = await db.scalar(select(RefreshToken).where(RefreshToken.jti == jti))
        if rt and rt.revoked_at is None:
            rt.revoked_at = datetime.now(timezone.utc)
            await db.commit()
