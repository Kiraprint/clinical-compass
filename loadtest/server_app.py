"""Uvicorn-entrypoint для нагрузочного стенда (не входит в прод-образ).

Опционально переопределяет размер пула SQLAlchemy через
CC_LOADTEST_POOL_SIZE — чтобы измерить, снимает ли увеличение пула обрыв на
высокой конкуренции.
"""

from __future__ import annotations

import os

import uvicorn

_pool_size = int(os.environ.get("CC_LOADTEST_POOL_SIZE", "0"))
if _pool_size:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.db as db
    from app.config import settings

    def _setup(database_url: str | None = None) -> None:
        db.engine = create_engine(
            database_url or settings.database_url,
            pool_pre_ping=True,
            pool_size=_pool_size,
            max_overflow=max(1, _pool_size // 4),
        )
        db.SessionLocal = sessionmaker(
            bind=db.engine, autoflush=False, expire_on_commit=False
        )

    db.setup = _setup

uvicorn.run(
    "app.main:app",
    host="127.0.0.1",
    port=int(os.environ.get("CC_LOADTEST_PORT", "8011")),
    log_level="warning",
    access_log=False,
)
