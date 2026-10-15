"""Alembic env: связывает конфиг с метаданными моделей проекта."""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Гарантируем, что корень пакета app виден (работаем из /app/api или api/).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.config import settings as app_settings  # noqa: E402

# Регистрируем модели в Base.metadata (иначе autogenerate видит пустую схему).
from app import models  # noqa: E402,F401

config = context.config

# Приоритет URL: явный (init_db() -> command.upgrade) > CC_DATABASE_URL > settings.
# Так тесты (compass_test) и прод (compass) используют свою БД, а не хардкод ini.
if not config.get_main_option("sqlalchemy.url", None):
    database_url = os.environ.get("CC_DATABASE_URL", app_settings.database_url)
    config.set_main_option("sqlalchemy.url", database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = db.Base.metadata


def run_migrations_offline() -> None:
    """Запуск без подключения к БД: генерируем SQL."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Запуск в живом соединении."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
