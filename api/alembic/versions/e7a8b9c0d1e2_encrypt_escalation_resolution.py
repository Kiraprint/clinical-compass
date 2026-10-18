"""encrypt escalations.resolution free-text PHI at rest (re-audit F1)

Revision ID: e7a8b9c0d1e2
Revises: f7a8b9c0d1e2
Create Date: 2026-10-19

F1 re-audit: escalations.resolution (свободный текст — резолюция клинициста по
эскалации) остался plaintext в миграции e6f7a8b9c0d1 (применена к live-БД с 4 колонками).
Эта миграция шифрует пятую free-text колонку тем же механизмом pgp_sym_encrypt.
Ключ — CC_DATA_KEY (env), литерал с экранированием (bind-param в ALTER TYPE USING
не работает, см. e6f7a8b9c0d1). Одноразовая миграция, ключ виден в pg_stat_activity
только на время выполнения.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "e7a8b9c0d1e2"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    from app.crypto import data_key
    k = data_key().replace("'", "''")
    op.execute(
        text(
            "ALTER TABLE escalations ALTER COLUMN resolution TYPE bytea "
            f"USING pgp_sym_encrypt(resolution, '{k}')"
        )
    )


def downgrade() -> None:
    from app.crypto import data_key
    k = data_key().replace("'", "''")
    op.execute(
        text(
            "ALTER TABLE escalations ALTER COLUMN resolution TYPE text "
            f"USING pgp_sym_decrypt(resolution, '{k}')"
        )
    )
