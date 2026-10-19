"""encrypt adherence_records.medication (treatment context) at rest

Revision ID: e8b9c0d1e2f3
Revises: e7a8b9c0d1e2
Create Date: 2026-10-19

F1 re-audit round 3: adherence_records.medication — свободный текст (название
препарата), раскрывающий контекст лечения/диагноза. Шифруется тем же механизмом
pgp_sym_encrypt, что и остальные free-text ПДн колонки.
Ключ — CC_DATA_KEY; литерал с экранированием (bind-param в ALTER TYPE USING
не работает). Одноразовая миграция.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "e8b9c0d1e2f3"
down_revision = "e7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    from app.crypto import data_key
    k = data_key().replace("'", "''")
    op.execute(
        text(
            "ALTER TABLE adherence_records ALTER COLUMN medication TYPE bytea "
            f"USING pgp_sym_encrypt(medication, '{k}')"
        )
    )


def downgrade() -> None:
    from app.crypto import data_key
    k = data_key().replace("'", "''")
    op.execute(
        text(
            "ALTER TABLE adherence_records ALTER COLUMN medication TYPE varchar(128) "
            f"USING pgp_sym_decrypt(medication, '{k}')"
        )
    )
