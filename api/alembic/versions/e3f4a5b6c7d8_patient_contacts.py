"""patient contacts: users.phone (encrypted) + users.birth_date

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-10-22

FR-22: контакты пациента в карточке врача. phone — ПДн, хранится как bytea
(pgp_sym_encrypt, см. api/app/crypto.EncryptedText); birth_date — календарная
дата, отдельная колонка date (обоснование открытого хранения — docs/02 §5).
Обе колонки nullable: контакты необязательны и уничтожаются при отзыве согласия
(users.phone → NULL, users.birth_date → NULL).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e3f4a5b6c7d8"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    # bytea — как у остальных зашифрованных ПДн-колонок (EncryptedText: DDL bytea).
    op.execute("ALTER TABLE users ADD COLUMN phone bytea")
    op.add_column("users", sa.Column("birth_date", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "birth_date")
    op.drop_column("users", "phone")
