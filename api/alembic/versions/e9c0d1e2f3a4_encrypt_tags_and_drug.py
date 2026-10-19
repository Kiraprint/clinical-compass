"""encrypt diary tags + adverse-event suspected_drug (round-4 NEW-2/NEW-3)

Revision ID: e9c0d1e2f3a4
Revises: e8b9c0d1e2f3
Create Date: 2026-10-19

NEW-2: diary_entries.tags (JSONB) — свободный текст пациента («паника»,
«бессонница»), спецкатегория ПДн. Кодируется в JSON-строку и шифруется pgcrypto.
NEW-3: adverse_events.suspected_drug (varchar) — контекст лечения, тот же класс.

Ключ — CC_DATA_KEY; литерал с экранированием (bind-param в ALTER TYPE USING
не работает). Одноразовая миграция.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "e9c0d1e2f3a4"
down_revision = "e8b9c0d1e2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    from app.crypto import data_key
    k = data_key().replace("'", "''")
    # NEW-2: JSONB → bytea (JSON-текст шифруется целиком); пустой список — не NULL.
    op.execute("UPDATE diary_entries SET tags = '[]'::jsonb WHERE tags IS NULL")
    op.execute(
        text(
            "ALTER TABLE diary_entries ALTER COLUMN tags TYPE bytea "
            f"USING pgp_sym_encrypt(tags::text, '{k}')"
        )
    )
    op.execute("ALTER TABLE diary_entries ALTER COLUMN tags SET NOT NULL")
    # NEW-3: varchar → bytea
    op.execute(
        text(
            "ALTER TABLE adverse_events ALTER COLUMN suspected_drug TYPE bytea "
            f"USING pgp_sym_encrypt(suspected_drug, '{k}')"
        )
    )


def downgrade() -> None:
    from app.crypto import data_key
    k = data_key().replace("'", "''")
    op.execute(
        text(
            "ALTER TABLE adverse_events ALTER COLUMN suspected_drug TYPE varchar(128) "
            f"USING pgp_sym_decrypt(suspected_drug, '{k}')"
        )
    )
    op.execute(
        text(
            "ALTER TABLE diary_entries ALTER COLUMN tags TYPE jsonb "
            f"USING pgp_sym_decrypt(tags, '{k}')::jsonb"
        )
    )
