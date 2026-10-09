# E3: auth. Контракт: docs/04 §3.1; тесты — docs/06 §3.
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import security
from app.db import get_db
from app.errors import Problem
from app.models import User

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
    refresh_token: str


@router.post("/register", status_code=201)
def register(body: RegisterBody, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    exists = db.scalar(select(User).where(User.email == email))
    if exists:
        raise Problem(409, "CONFLICT", "Пользователь с таким email уже зарегистрирован")
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
def login(body: LoginBody, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    if user is None or not security.verify_password(body.password, user.password_hash):
        raise Problem(401, "UNAUTHORIZED", "Неверный email или пароль")
    if user.role != "PATIENT":
        if not user.totp_secret or not body.otp or not security.verify_totp(user.totp_secret, body.otp):
            raise Problem(401, "UNAUTHORIZED", "Требуется корректный код 2FA")
    return {
        "access_token": security.create_access_token(user.id, user.role),
        "refresh_token": security.create_refresh_token(user.id),
        "role": user.role,
    }


@router.post("/refresh")
def refresh(body: RefreshBody, db: Session = Depends(get_db)):
    try:
        payload = security.decode_token(body.refresh_token)
    except Exception:
        raise Problem(401, "TOKEN_EXPIRED", "Refresh-токен недействителен или истёк")
    if payload.get("type") != "refresh":
        raise Problem(401, "UNAUTHORIZED", "Ожидается refresh-токен")
    user = db.get(User, uuid.UUID(payload["sub"]))
    if user is None:
        raise Problem(401, "UNAUTHORIZED", "Пользователь не найден")
    return {"access_token": security.create_access_token(user.id, user.role)}