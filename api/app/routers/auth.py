# E3: auth. Контракт: docs/04 §3.1; тесты — docs/06 §3.
from __future__ import annotations

import uuid

from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import security
from app.config import settings
from app.db import get_db
from app.errors import Problem
from app.models import RefreshToken, User
from app.ratelimit import check_rate, record_rate_hit

router = APIRouter(prefix="/auth", tags=["auth"])

ALLOWED_ROLES = ("PATIENT", "CLINICIAN", "ADMIN")

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

@router.get("/config")
def auth_config():
    """Публичная конфигурация входа (без токена): CC_REQUIRE_OTP для UI."""
    return {"require_otp": settings.require_otp}

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

def _privileged_registration_allowed(request: Request, db: Session) -> bool:
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
    actor = db.get(User, uuid.UUID(payload["sub"]))
    return actor is not None and actor.role == "ADMIN"


@router.post("/register", status_code=201)
def register(body: RegisterBody, request: Request, db: Session = Depends(get_db)):
    """Регистрация пользователя.

    NEW-1 (HIGH): публичная регистрация привилегированных ролей (CLINICIAN/ADMIN)
    запрещена вне демо-режима — иначе любой неаутентифицированный пользователь
    создаёт себе ADMIN, выдаёт ростер и читает расшифрованные карты пациентов.
    Демо-стенд разрешает это явно (`CC_ALLOW_DEMO_SECRETS=true`); прод требует
    создания привилегированных ролей аутентифицированным ADMIN.
    """
    email = body.email.strip().lower()
    exists = db.scalar(select(User).where(User.email == email))
    if exists:
        raise Problem(409, "CONFLICT", "Пользователь с таким email уже зарегистрирован")
    if body.role != "PATIENT" and not _privileged_registration_allowed(request, db):
        raise Problem(
            403, "REGISTRATION_FORBIDDEN",
            "Создание роли CLINICIAN/ADMIN требует аутентифицированного администратора",
        )
    user = User(email=email, password_hash=security.hash_password(body.password), role=body.role)
    totp_secret: str | None = None
    if body.role != "PATIENT":
        totp_secret = security.generate_totp_secret()
        user.totp_secret = totp_secret
    db.add(user)
    db.commit()
    resp: dict = {"id": str(user.id), "email": user.email, "role": user.role}
    if totp_secret:
        resp["totp_secret"] = totp_secret
    return resp

@router.post("/login")
def login(body: LoginBody, request: Request, response: Response, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    client = request.client.host if request.client else "unknown"
    allowed, retry_after = check_rate((client, email))
    if not allowed:
        raise Problem(429, "TOO_MANY_REQUESTS",
                      f"Слишком много попыток входа, повторите через {retry_after} с")
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not security.verify_password(body.password, user.password_hash):
        record_rate_hit((client, email))
        raise Problem(401, "UNAUTHORIZED", "Неверный email или пароль")
    if user.role != "PATIENT" and settings.require_otp:
        if not user.totp_secret or not body.otp or not security.verify_totp(user.totp_secret, body.otp):
            record_rate_hit((client, email))
            raise Problem(401, "UNAUTHORIZED", "Требуется корректный код 2FA")
    jti = _issue_jti(db, user.id)
    db.commit()
    access = security.create_access_token(user.id, user.role)
    refresh = security.create_refresh_token(user.id, jti)
    _set_auth_cookies(response, access, refresh)  # F9: httpOnly cookies
    return {"access_token": access, "refresh_token": refresh, "role": user.role}

def _refresh_delta():
    from datetime import timedelta
    return timedelta(days=settings.refresh_ttl_days)

def _issue_jti(db: Session, user_id) -> str:
    """Создать строку RefreshToken и вернуть jti для JWT."""
    jti = uuid.uuid4().hex
    db.add(RefreshToken(jti=jti, user_id=user_id,
                        expires_at=datetime.now(timezone.utc) + _refresh_delta()))
    db.flush()
    return jti

@router.post("/refresh")
def refresh(body: RefreshBody | None = None, request: Request = None, response: Response = None, db: Session = Depends(get_db)):
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
    rt = db.scalar(
        select(RefreshToken).where(RefreshToken.jti == jti).with_for_update()
    )
    if rt is None:
        raise Problem(401, "TOKEN_EXPIRED", "Refresh-токен не найден в сторе")
    if rt.revoked_at is not None:
        raise Problem(401, "TOKEN_EXPIRED", "Refresh-токен отозван")
    if rt.expires_at < datetime.now(timezone.utc):
        raise Problem(401, "TOKEN_EXPIRED", "Refresh-токен истёк")
    user = db.get(User, rt.user_id)
    if user is None:
        raise Problem(401, "UNAUTHORIZED", "Пользователь не найден")
    # ротация: отозвать старый, создать новый
    rt.revoked_at = datetime.now(timezone.utc)
    new_jti = _issue_jti(db, user.id)
    db.commit()
    access = security.create_access_token(user.id, user.role)
    new_refresh = security.create_refresh_token(user.id, new_jti)
    _set_auth_cookies(response, access, new_refresh)  # F9: rotate cookies
    return {"access_token": access, "refresh_token": new_refresh}

@router.post("/logout", status_code=204)
def logout(body: RefreshBody | None = None, request: Request = None, response: Response = None, db: Session = Depends(get_db)):
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
        rt = db.scalar(select(RefreshToken).where(RefreshToken.jti == jti))
        if rt and rt.revoked_at is None:
            rt.revoked_at = datetime.now(timezone.utc)
            db.commit()
