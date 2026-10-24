"""Dependencies: текущий пользователь по JWT, RBAC-проверки."""

from __future__ import annotations

import uuid

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from . import security
from .db import get_db
from .errors import Problem
from .models import User

ROLES = ("PATIENT", "CLINICIAN", "ADMIN")

async def get_current_user(
    authorization: str = Header(default=""),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
) -> User:
    """F9: токен из Authorization header (API-клиенты/e2e) ИЛИ из cc_access cookie (UI).
    httpOnly cookie недоступен из JS → XSS не может украсть токен."""
    token = ""
    if authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ")
    elif request is not None:
        token = request.cookies.get("cc_access", "")
    if not token:
        raise Problem(401, "UNAUTHORIZED", "Отсутствует токен (Authorization или cookie)")
    try:
        payload = security.decode_token(token)
    except Exception:
        raise Problem(401, "TOKEN_EXPIRED", "Токен недействителен или истёк")
    if payload.get("type") != "access":
        raise Problem(401, "UNAUTHORIZED", "Ожидается access-токен")
    user = await db.get(User, uuid.UUID(payload["sub"]))
    if user is None:
        raise Problem(401, "UNAUTHORIZED", "Пользователь не найден")
    return user

def require_roles(*roles: str):
    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise Problem(403, "ROLE_MISMATCH", f"Требуется роль: {', '.join(roles)}")
        return user

    return checker

require_patient = require_roles("PATIENT")
require_clinician = require_roles("CLINICIAN")
require_admin = require_roles("ADMIN")

async def require_consent_if_patient(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> User:
    """F2 (152-ФЗ ст.9 ч.2-3): для PATIENT — активное согласие обязательно.
    CLINICIAN/ADMIN проходят: их действия не обрабатывают спецкатегорию пациента
    (пациент-специфичные чтения врача отдельно roster-гейтятся и аудируются).
    Нужен для push/notifications: после отзыва согласия пациент не должен
    восстанавливать каналы доставки или читать уведомления.
    """
    if user.role != "PATIENT":
        return user
    from sqlalchemy import select

    from .models import Consent

    active = await db.scalar(select(Consent).where(Consent.user_id == user.id, Consent.revoked_at.is_(None)))
    if active is None:
        raise Problem(403, "CONSENT_REQUIRED", "Требуется активное согласие на обработку персональных данных (152-ФЗ)")
    return user


async def require_consent(user: User = Depends(require_patient), db: AsyncSession = Depends(get_db)) -> User:
    """152-ФЗ: без активного согласия — никаких персональных данных."""
    from sqlalchemy import select

    from .models import Consent

    active = await db.scalar(select(Consent).where(Consent.user_id == user.id, Consent.revoked_at.is_(None)))
    if active is None:
        raise Problem(403, "CONSENT_REQUIRED", "Требуется активное согласие на обработку персональных данных (152-ФЗ)")
    return user
