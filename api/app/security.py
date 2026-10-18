"""Безопасность: argon2id-пароли, JWT (HS256), TOTP 2FA."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

from .config import settings

if os.environ.get("CC_FAST_ARGON2") == "1":  # тесты: снижаем стоимость хеширования
    _hasher = PasswordHasher(time_cost=1, memory_cost=16384, parallelism=2)
else:
    _hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name="Clinical Compass")


def verify_totp(secret: str, otp: str) -> bool:
    # valid_window=1 — допуск ±1 30-секундного окна (RFC 6238 §5.2): код, вычисленный
    # в конце окна, остаётся валидным в начале следующего. Устраняет 401-флейк при
    # входе через UI/e2e, когда TOTP считается перед заполнением, а валидируется
    # после клика «Войти» (страдает граница окна). Безопасность: 3 валидных кода из
    # 10^6, компенсируется rate-limiter'ом 5/60с на (IP, email).
    return pyotp.TOTP(secret).verify(otp, valid_window=1)


def create_access_token(user_id: uuid.UUID, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "role": role,
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_ttl_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def create_refresh_token(user_id: uuid.UUID, jti: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "type": "refresh",
        "jti": jti,
        "iat": now,
        "exp": now + timedelta(days=settings.refresh_ttl_days),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])