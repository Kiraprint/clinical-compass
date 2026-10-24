"""БД: async engine для API, sync engine для alembic/CLI/схемы тестов, триггер аудита.

Всё, что исполняется в запросе FastAPI, идёт через `engine` (async, psycopg async
dialect) и `AsyncSessionLocal`. Синхронный `sync_engine`/`SyncSessionLocal` живут
только для alembic, seed-скриптов и служебной подготовки схемы в тестах: их нельзя
использовать в request-path, иначе поток AnyIO снова блокируется на драйвере.
"""

from __future__ import annotations

import os
import time
from typing import AsyncIterator

import anyio
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

from .config import settings


class Base(DeclarativeBase):
    pass


# Async (API). Наполняется setup().
engine: AsyncEngine | None = None
AsyncSessionLocal: async_sessionmaker[AsyncSession] | None = None

# Sync (alembic / seed / схема тестов). Наполняется setup().
sync_engine = None
SyncSessionLocal = None


def worker_count() -> int:
    """Число процессов uvicorn (WEB_CONCURRENCY). Каждый процесс держит свой пул."""
    try:
        return max(1, int(os.environ.get("WEB_CONCURRENCY", "1")))
    except ValueError:
        return 1


def pool_capacity(profile: str = "api") -> int:
    """Сколько соединений один процесс может занять одновременно (pool+overflow).

    `profile="api"` — процесс uvicorn; `profile="background"` — воркер
    напоминаний и сервис обогащения КБ: им веер соединений не нужен, поэтому
    их пул меньше и считается отдельно в бюджете Postgres.
    """
    if profile == "background":
        return settings.db_bg_pool_size + settings.db_bg_max_overflow
    return settings.db_pool_size + settings.db_max_overflow


_SYNC_ALLOWANCE = 2  # init_db/alembic + возможное соединение sync-сессии (seed/CLI)


def validate_pool_budget(workers: int | None = None, background: int | None = None) -> None:
    """Fail-fast: суммарный спрос пулов не должен превышать CC_DB_MAX_CONNECTIONS.

    max_connections Postgres — общий ресурс инсталляции: его делят все процессы
    API, фоновые сервисы (напоминания, обогащение КБ), CLI и psql/миграции.
    Поэтому считаем оба вида процессов: `workers × (api-пул + служебное
    соединение) + background × (bg-пул + служебное)`. При дефолтах
    20+20 на воркер и 2+0 на фоновый процесс потолок 100 выдерживают
    2 воркера и 2 фоновых сервиса (2*42 + 2*4 = 92).
    """
    workers = worker_count() if workers is None else workers
    background = settings.db_background_processes if background is None else background
    demand = workers * (pool_capacity() + _SYNC_ALLOWANCE) + background * (
        pool_capacity("background") + _SYNC_ALLOWANCE
    )
    if demand > settings.db_max_connections:
        raise RuntimeError(
            f"Пул БД не влезает в Postgres: {workers} × "
            f"(CC_DB_POOL_SIZE={settings.db_pool_size} + "
            f"CC_DB_MAX_OVERFLOW={settings.db_max_overflow} + {_SYNC_ALLOWANCE} служебных) + "
            f"{background} фоновых × (CC_DB_BG_POOL_SIZE={settings.db_bg_pool_size} + "
            f"CC_DB_BG_MAX_OVERFLOW={settings.db_bg_max_overflow} + {_SYNC_ALLOWANCE}) = {demand} > "
            f"CC_DB_MAX_CONNECTIONS={settings.db_max_connections}. "
            "Уменьшите CC_DB_POOL_SIZE/CC_DB_MAX_OVERFLOW, WEB_CONCURRENCY или "
            "CC_DB_BACKGROUND_PROCESSES."
        )


def setup(database_url: str | None = None, *, poolclass=None, profile: str = "api") -> None:
    """Инициализирует оба engine'а. poolclass=NullPool — для тестов.

    Тесты гоняют TestClient с отдельным event loop на каждый тест; пул,
    созданный в чужом цикле, нельзя переиспользовать, поэтому conftest
    просит NullPool (соединение на checkout, без удержания между запросами).

    `profile="background"` даёт маленький пул (CC_DB_BG_POOL_SIZE) — так
    фоновые процессы не съедают бюджет соединений, рассчитанный на API.
    """
    global engine, AsyncSessionLocal, sync_engine, SyncSessionLocal
    url = database_url or settings.database_url

    if poolclass is None:
        if profile == "background":
            async_kwargs: dict = {
                "pool_pre_ping": True,
                "pool_size": settings.db_bg_pool_size,
                "max_overflow": settings.db_bg_max_overflow,
                "pool_timeout": settings.db_pool_timeout,
            }
        else:
            async_kwargs = {
                "pool_pre_ping": True,
                "pool_size": settings.db_pool_size,
                "max_overflow": settings.db_max_overflow,
                "pool_timeout": settings.db_pool_timeout,
            }
    else:
        async_kwargs = {"pool_pre_ping": True, "poolclass": poolclass}

    engine = create_async_engine(url, **async_kwargs)
    AsyncSessionLocal = async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    # Sync-движок — только alembic/seed/CLI/подготовка схемы в тестах, никогда
    # request-path. NullPool: служебные соединения не должны висеть в пуле
    # процесса и выпадать из бюджета max_connections.
    sync_engine = create_engine(url, pool_pre_ping=True, poolclass=NullPool)
    SyncSessionLocal = sessionmaker(bind=sync_engine, autoflush=False, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """Одна сессия на запрос. Закрытие/откат гарантированы в любом исходе.

    Shield нужен на отмене (клиент отвалился): без него await в finally
    прерывается и соединение остаётся занятым до сборки мусора.
    """
    session = AsyncSessionLocal()
    try:
        yield session
    finally:
        with anyio.CancelScope(shield=True):
            try:
                if session.in_transaction():
                    await session.rollback()
            finally:
                await session.close()


def install_audit_trigger(engine_=None) -> None:
    with (engine_ or sync_engine).begin() as conn:
        conn.execute(text(
            "CREATE OR REPLACE FUNCTION forbid_audit_mutation() RETURNS trigger AS $$"
            " BEGIN RAISE EXCEPTION 'audit_log is append-only'; END; $$ LANGUAGE plpgsql"
        ))
        conn.execute(text(
            "CREATE OR REPLACE TRIGGER trg_audit_no_upd"
            " BEFORE UPDATE OR DELETE ON audit_log"
            " FOR EACH ROW EXECUTE FUNCTION forbid_audit_mutation()"
        ))
        conn.execute(text(
            "CREATE OR REPLACE FUNCTION forbid_audit_truncate() RETURNS trigger AS $$"
            " BEGIN RAISE EXCEPTION 'audit_log is append-only (TRUNCATE forbidden)'; END;"
            " $$ LANGUAGE plpgsql"
        ))
        conn.execute(text(
            "CREATE OR REPLACE TRIGGER trg_audit_no_truncate"
            " BEFORE TRUNCATE ON audit_log"
            " FOR EACH STATEMENT EXECUTE FUNCTION forbid_audit_truncate()"
        ))


_MIGRATION_LOCK = "cc_alembic_migrations"
_MIGRATION_WAIT_SECONDS = 60


def init_db() -> None:
    """Приводим схему к актуальной: alembic upgrade head + гарантия триггера аудита.

    create_all здесь намеренно не используется: он не альтерирует существующие
    таблицы, из-за чего демо-БД ловила устаревшую схему (недостающие индексы
    runs_checkin_daily/audit_log_occurred/escalations_patient и CHECK-ограничения).
    Единственный источник истины — миграции в api/alembic.

    Адvisory-лок: при `--workers N` каждый процесс идёт в lifespan и вызывает
    init_db, поэтому холодная БД получала N гонок CREATE TABLE. Лок берём на
    отдельном соединении (alembic открывает своё), чтобы миграции шли строго
    по очереди; второй процесс дождётся и увидит уже актуальную версию.
    """
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    from . import models  # noqa: F401  (регистрация моделей в метаданных)

    api_dir = Path(__file__).resolve().parent.parent
    cfg = Config(str(api_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(api_dir / "alembic"))
    # URL берём из уже настроенного engine (соответствует CC_DATABASE_URL).
    # str(url) маскирует пароль как *** — нужен render_as_string без маски.
    cfg.set_main_option("sqlalchemy.url", sync_engine.url.render_as_string(hide_password=False))
    with sync_engine.connect() as lock_conn:
        # Ждём лок не бесконечно: если предшественник упал, соединение закрыто —
        # Postgres снимает лок сам, но подвисший DDL лучше встретить ошибкой старта.
        got = lock_conn.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": _MIGRATION_LOCK}
        ).scalar()
        waited = 0
        while not got and waited < _MIGRATION_WAIT_SECONDS:
            lock_conn.commit()
            time.sleep(1)
            waited += 1
            got = lock_conn.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": _MIGRATION_LOCK}
            ).scalar()
        if not got:
            raise RuntimeError(
                "не удалось взять advisory-лок миграций за "
                f"{_MIGRATION_WAIT_SECONDS} с: другой процесс держит его слишком долго"
            )
        lock_conn.commit()  # не держим открытую транзакцию, пока alembic делает DDL
        try:
            command.upgrade(cfg, "head")
            install_audit_trigger(sync_engine)
        finally:
            try:
                lock_conn.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": _MIGRATION_LOCK}
                )
                lock_conn.commit()
            except Exception:  # noqa: BLE001 — закрытие соединения снимет лок само
                lock_conn.invalidate()


def truncate_all() -> None:
    """Управляемый сброс (тесты / seed --reset): снимаем TRUNCATE-гард, чистим, возвращаем.

    Прямой SQL-обход (TRUNCATE audit_log вручную) по-прежнему запрещён триггером.
    """
    with sync_engine.begin() as conn:
        conn.execute(text("DROP TRIGGER IF EXISTS trg_audit_no_truncate ON audit_log"))
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        conn.execute(text(
            "CREATE OR REPLACE FUNCTION forbid_audit_truncate() RETURNS trigger AS $$"
            " BEGIN RAISE EXCEPTION 'audit_log is append-only (TRUNCATE forbidden)'; END;"
            " $$ LANGUAGE plpgsql"
        ))
        conn.execute(text(
            "CREATE OR REPLACE TRIGGER trg_audit_no_truncate"
            " BEFORE TRUNCATE ON audit_log"
            " FOR EACH STATEMENT EXECUTE FUNCTION forbid_audit_truncate()"
        ))
