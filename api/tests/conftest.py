"""Интеграционные тесты: реальный Postgres (compass_test), HTTP через TestClient.

Контракт: docs/06-TDD.md §3. Без мок-эхо: только поведение контрактов.
"""

from __future__ import annotations

import os

os.environ.setdefault("CC_FAST_ARGON2", "1")

import pyotp
import pytest
from fastapi.testclient import TestClient

from app import db

TEST_URL = os.environ.get(
    "CC_TEST_DATABASE_URL", "postgresql+psycopg://compass:compass@127.0.0.1:5432/compass_test"
)


@pytest.fixture(scope="session")
def engine():
    from app import models  # noqa: F401 — регистрирует таблицы в Base.metadata

    db.setup(TEST_URL)
    db.Base.metadata.drop_all(db.engine)  # свежая схема: create_all не альтерирует таблицы
    db.init_db()
    yield db.engine
    db.engine.dispose()


@pytest.fixture()
def client(engine):
    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean(engine):
    yield
    db.truncate_all()


def register(client, email, password="Str0ng-Pass!", role="PATIENT") -> dict:
    resp = client.post("/api/v1/auth/register", json={"email": email, "password": password, "role": role})
    assert resp.status_code == 201, resp.text
    return resp.json()


def login(client, email, password="Str0ng-Pass!", otp: str | None = None) -> dict:
    body = {"email": email, "password": password}
    if otp is not None:
        body["otp"] = otp
    resp = client.post("/api/v1/auth/login", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def patient_ctx(client, email: str, password: str = "Str0ng-Pass!") -> tuple[dict, str]:
    """PATIENT с активным согласием (152-ФЗ). Возвращает (заголовки, user_id)."""
    reg = register(client, email, password)
    data = login(client, email, password)
    h = auth_header(data["access_token"])
    resp = client.post("/api/v1/consent", json={"version": 1}, headers=h)
    assert resp.status_code == 201, resp.text
    return h, reg["id"]


def clinician_ctx(client, email: str, password: str = "Str0ng-Pass!") -> tuple[dict, str]:
    """CLINICIAN с 2FA. Возвращает (заголовки, user_id)."""
    reg = register(client, email, password, role="CLINICIAN")
    data = login(client, email, password, otp=pyotp.TOTP(reg["totp_secret"]).now())
    return auth_header(data["access_token"]), reg["id"]


def admin_ctx(client, email: str, password: str = "Str0ng-Pass!") -> tuple[dict, str]:
    """ADMIN с 2FA. Возвращает (заголовки, user_id)."""
    reg = register(client, email, password, role="ADMIN")
    data = login(client, email, password, otp=pyotp.TOTP(reg["totp_secret"]).now())
    return auth_header(data["access_token"]), reg["id"]


def finish_screening(client, h, answers: dict, code: str = "PHQ-9") -> dict:
    """Полный скрининг: создание → ответы → finish. Возвращает assessment."""
    run = client.post("/api/v1/screenings", json={"questionnaire_code": code}, headers=h)
    assert run.status_code == 201, run.text
    run_id = run.json()["run_id"]
    assert client.post(f"/api/v1/runs/{run_id}/answers", json={"answers": answers}, headers=h).status_code == 204
    resp = client.post(f"/api/v1/runs/{run_id}/finish", headers=h)
    assert resp.status_code == 201, resp.text
    return resp.json()