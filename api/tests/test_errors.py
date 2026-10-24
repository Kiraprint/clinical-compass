"""Контракт ошибок API вне доменных Problem: исчерпание пула БД.

SQLAlchemy TimeoutError (пул занят дольше CC_DB_POOL_TIMEOUT) раньше уходил
в общий 500 — клиент не понимал, что это временная перегрузка.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.errors import register_handlers


def test_pool_exhaustion_is_503_with_retry_after():
    app = FastAPI()
    register_handlers(app)

    @app.get("/needs-db")
    async def needs_db():
        raise PoolTimeoutError(
            "QueuePool limit of size 20 overflow 20 reached, connection timed out"
        )

    with TestClient(app) as client:
        response = client.get("/needs-db")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "2"
    body = response.json()
    assert body["code"] == "DB_UNAVAILABLE"
    assert body["title"] == body["code"]
    assert body["instance"] == "/needs-db"
