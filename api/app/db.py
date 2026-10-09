"""БД: engine, session, Base, привязка триггера аудита (append-only)."""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


engine = None
SessionLocal = None


def setup(database_url: str | None = None) -> None:
    global engine, SessionLocal
    engine = create_engine(database_url or settings.database_url, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def install_audit_trigger(engine_=None) -> None:
    with (engine_ or engine).begin() as conn:
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


def init_db() -> None:
    from . import models  # noqa: F401  (регистрация моделей в метаданных)
    Base.metadata.create_all(engine)
    install_audit_trigger(engine)


def truncate_all() -> None:
    """Управляемый сброс (тесты / seed --reset): снимаем TRUNCATE-гард, чистим, возвращаем.

    Прямой SQL-обход (TRUNCATE audit_log вручную) по-прежнему запрещён триггером.
    """
    with engine.begin() as conn:
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