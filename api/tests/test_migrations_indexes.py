"""Миграции: композитные индексы списков откатываются и накатываются обратно.

Контракт docs/06 §3. Проверяем на реальном Postgres: upgrade → downgrade -1 →
upgrade не ломает схему, единственный head и ровно один вариант каждого индекса.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from app import db

API_DIR = Path(__file__).resolve().parent.parent


def _config() -> Config:
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db.sync_engine.url.render_as_string(hide_password=False))
    return cfg


def _indexes() -> set[str]:
    insp = inspect(db.sync_engine)
    return {
        index["name"]
        for table in ("notifications", "visits", "patient_medications")
        for index in insp.get_indexes(table)
    }


def test_single_head():
    heads = ScriptDirectory.from_config(_config()).get_heads()
    assert len(heads) == 1, heads


def test_index_migration_roundtrip(engine):
    cfg = _config()
    command.upgrade(cfg, "head")
    up = _indexes()
    assert {"ix_notifications_user_created", "ix_patient_medications_patient_status"} <= up
    # Одноколоночные индексы, вошедшие в новый как префикс, удалены — дублей нет.
    assert "ix_notifications_user_id" not in up
    assert "ix_patient_medications_patient_id" not in up

    command.downgrade(cfg, "-1")
    down = _indexes()
    assert "ix_notifications_user_created" not in down
    assert "ix_patient_medications_patient_status" not in down
    assert {"ix_notifications_user_id", "ix_patient_medications_patient_id"} <= down

    command.upgrade(cfg, "head")
    again = _indexes()
    assert {"ix_notifications_user_created", "ix_patient_medications_patient_status"} <= again
    assert "ix_notifications_user_id" not in again
