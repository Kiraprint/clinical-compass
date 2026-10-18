"""encrypt free-text special-category PDn at rest (pgcrypto)

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-10-19

F1 (152-ФЗ art.10; ПП РФ №1119 УЗ-2; ФСТЭК №21): free-text спецкатегория —
diary_entries.content, chat_messages.content, adverse_events.event_text,
adherence_records.note — хранится как bytea (pgp_sym_encrypt), читается через
pgp_sym_decrypt (см. api/app/crypto.EncryptedText). Ключ — CC_DATA_KEY (прод — KMS),
параметризованный bind (не в тексте SQL). Существующие строки шифруются в USING.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text


revision = "e6f7a8b9c0d1"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


_COLS = [
    ("diary_entries", "content"),
    ("chat_messages", "content"),
    ("adverse_events", "event_text"),
    ("adherence_records", "note"),
]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    from app.crypto import data_key
    # bind-param в ALTER TYPE USING не работает (PG не выводит тип $1 в DDL-контексте),
    # поэтому ключ — литерал с экранированием кавычек. Ключ из CC_DATA_KEY (env, не git);
    # миграция одноразовая, ключ виден в pg_stat_activity только на время её выполнения.
    k = data_key().replace("'", "''")
    for table, col in _COLS:
        op.execute(
            text(
                f"ALTER TABLE {table} ALTER COLUMN {col} TYPE bytea "
                f"USING pgp_sym_encrypt({col}, '{k}')"
            )
        )


def downgrade() -> None:
    from app.crypto import data_key
    k = data_key().replace("'", "''")
    for table, col in _COLS:
        op.execute(
            text(
                f"ALTER TABLE {table} ALTER COLUMN {col} TYPE text "
                f"USING pgp_sym_decrypt({col}, '{k}')"
            )
        )
