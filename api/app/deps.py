"""Dependencies: текущий пользователь по JWT, RBAC-проверки."""

from __future__ import annotations

import uuid

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from . import security
from .db import get_db
from .errors import Problem
from .models import User

ROLES = ("PATIENT", "CLINICIAN", "ADMIN")


def get_current_user(authorization: str = Header(default=""), db: Session = Depends(get_db)) -> User:
    if not authorization.startswith("Bearer "):
        raise Problem(401, "UNAUTHORIZED", "Отсутствует или неверный формат Authorization")
    token = authorization.removeprefix("Bearer ")
    try:
        payload = security.decode_token(token)
    except Exception:
        raise Problem(401, "TOKEN_EXPIRED", "Токен недействителен или истёк")
    if payload.get("type") != "access":
        raise Problem(401, "UNAUTHORIZED", "Ожидается access-токен")
    user = db.get(User, uuid.UUID(payload["sub"]))
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


def require_consent(user: User = Depends(require_patient), db: Session = Depends(get_db)) -> User:
    """152-ФЗ: без активного согласия — никаких персональных данных."""
    from sqlalchemy import select

    from .models import Consent

    active = db.scalar(select(Consent).where(Consent.user_id == user.id, Consent.revoked_at.is_(None)))
    if active is None:
        raise Problem(403, "CONSENT_REQUIRED", "Требуется активное согласие на обработку персональных данных (152-ФЗ)")
    return user