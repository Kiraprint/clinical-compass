"""Интеграционные тесты: реальный Postgres (compass_test), HTTP через TestClient.

Контракт: docs/06-TDD.md §3. Без мок-эхо: только поведение контрактов.
"""

from __future__ import annotations

import os

os.environ.setdefault("CC_FAST_ARGON2", "1")
os.environ.setdefault("CC_PUSH_ENABLED", "0")  # тесты без сетевого Web Push (уведомления в БД остаются)
os.environ.setdefault("CC_QDRANT_COLLECTION", "kb_test")  # не затираем демо-коллекцию kb
os.environ.setdefault("CC_DATA_KEY", "test-data-key-0123456789abcdef0123456789abcdef")  # F1: pgcrypto ключ для тестов
os.environ.setdefault("CC_ALLOW_DEMO_SECRETS", "true")  # F3: тесты используют демо-секреты

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
    from sqlalchemy import text

    db.setup(TEST_URL)
    # Полный сброс схемы: модельные таблицы + alembic_version (иначе upgrade head
    # увидит версию 'head' и не создаст таблицы, оставив БД пустой).
    with db.engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    db.init_db()  # alembic upgrade head + триггер аудита
    yield db.engine
    db.engine.dispose()


@pytest.fixture()
def client(engine):
    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c

@pytest.fixture()
def db_session(engine):
    """Сессия БД для прямых запросов в обход ORM (проверки шифрования в покое)."""
    with db.SessionLocal() as s:
        yield s
        s.rollback()


@pytest.fixture(autouse=True)
def clean(engine):
    # Справочник препаратов нужен build_corpus()/KB-тестам (kb_seed._drug_docs
    # читает из таблицы drugs). Сеем перед каждым тестом, т.к. teardown тронкает.
    from app.kb_seed import clear_corpus_cache
    from app.seed import _seed_drugs

    with db.SessionLocal() as s:
        _seed_drugs(s)
        s.commit()
    # Кеш корпуса живёт в процессе тестов: после пересева/тронка он устарел.
    clear_corpus_cache()
    yield
    from app.ratelimit import reset_rate_limits

    reset_rate_limits()
    clear_corpus_cache()
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


def finish_screening(client, h, answers: dict) -> dict:
    """Полный скрининг: создание → ответы → finish. Возвращает assessment.
    Шкала — серверный дефолт (PHQ-9), пациент её не передаёт (item 1)."""
    run = client.post("/api/v1/screenings", json={}, headers=h)
    assert run.status_code == 201, run.text
    run_id = run.json()["run_id"]
    assert client.post(f"/api/v1/runs/{run_id}/answers", json={"answers": answers}, headers=h).status_code == 204
    resp = client.post(f"/api/v1/runs/{run_id}/finish", headers=h)
    assert resp.status_code == 201, resp.text
    return resp.json()