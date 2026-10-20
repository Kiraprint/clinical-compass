"""Bootstrap-создание пользователя (NEW-1): единственный путь создать ADMIN/CLINICIAN
вне демо-режима, когда публичная регистрация привилегированных ролей запрещена.

Usage (в контейнере api или локально):
    python -m app.bootstrap_user --email admin@clinic.ru --role ADMIN [--password ...]
    python -m app.bootstrap_user --email doc@clinic.ru --role CLINICIAN

Пароль: --password, иначе CC_BOOTSTRAP_PASSWORD, иначе генерируется и печатается
один раз. TOTP-секрет печатается всегда (нужен для 2FA при первом входе).
Требует доступа к БД — т.е. запускается оператором, а не по сети.
"""
from __future__ import annotations

import argparse
import secrets
import sys

from sqlalchemy import select

from app import db as _db
from app import security
from app.config import settings
from app.models import User


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Создать пользователя вне веб-регистрации")
    ap.add_argument("--email", required=True)
    ap.add_argument("--role", required=True, choices=["PATIENT", "CLINICIAN", "ADMIN"])
    ap.add_argument("--password", default=None, help="иначе CC_BOOTSTRAP_PASSWORD или сгенерированный")
    args = ap.parse_args(argv)

    if _db.engine is None:
        _db.setup(settings.database_url)

    email = args.email.strip().lower()
    password = args.password or getattr(settings, "bootstrap_password", "") or secrets.token_urlsafe(18)
    generated = args.password is None and not getattr(settings, "bootstrap_password", "")

    with _db.SessionLocal() as s:
        if s.scalar(select(User).where(User.email == email)):
            print(f"[bootstrap] пользователь {email} уже существует", file=sys.stderr)
            return 1
        user = User(email=email, password_hash=security.hash_password(password), role=args.role)
        totp_secret = None
        if args.role != "PATIENT":
            totp_secret = security.generate_totp_secret()
            user.totp_secret = totp_secret
        s.add(user)
        s.commit()
        print(f"[bootstrap] создан {email} role={args.role}")
        if generated:
            print(f"[bootstrap] ПАРОЛЬ (показан один раз): {password}")
        if totp_secret:
            print(f"[bootstrap] TOTP-СЕКРЕТ (для 2FA): {totp_secret}")
        print("[bootstrap] смените пароль после первого входа")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
